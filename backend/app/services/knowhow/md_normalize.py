"""Excel 习惯排版 → 干净 CommonMark 的确定性规整器（零 LLM）。

也导出共享的 marker/缩进检测，供 content_signature 复用（单一真源），
以便「规整器判为 list marker 的字符」与「校验器剥离的字符」严格一致。
"""
from __future__ import annotations
import hashlib
import re
import unicodedata
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


# ---------------------------------------------------------------------------
# 单一围栏扫描器（模块内唯一一份）。CommonMark 行式规则：开口围栏 = 某行
# stripped 后以「>=3 个反引号或 >=3 个波浪线」的连续 run 开头（记录字符 + run
# 长度）；闭合围栏 = 同一字符、run 长度 >= 开口 run；未闭合则跑到输入末尾。
#
# 历史上这里有两份独立的围栏实现（这套行式 span 扫描 + `content_signature`
# 里的 `_FENCE_RE` DOTALL 正则），评审明确把「两份围栏实现」标为隐患：
# DOTALL 正则会「就近」配对三反引号，把内含 ``` 示例的 4 反引号块错切成碎
# 片，导致块内改动对签名隐形。现在收敛成这一份行式扫描器，`content_signature`
# 与（改动后仍需要围栏语义的）任何消费方都复用它。
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")


def _strip_container_prefix(line: str) -> str:
    """F4：剥掉行首的 CommonMark **blockquote 容器前缀**（`>` 后可选一个空格，每级
    前最多 3 个前导空格，可嵌套多级），供**签名侧**的围栏扫描器透视「嵌在引用块里
    的围栏」。非引用行原样返回。

    动机：`> ``` / `> a.b / `> ``` 这类引用块内的围栏，其 `>` 在 `.strip()` 后仍在，
    旧扫描器认不出围栏 -> 引用块内代码只拿到「放宽标点」的签名，`a.b`→`a-b`（`.`/`-`
    都是标点 P、被剥）蒙混过 `content_invariant`。剥掉 `>` 前缀后签名侧就能识别该围栏、
    把带前缀的整块纳入逐字/token 比对。

    仅签名/校验路径用（LLM 路径）——规则门 `_starts_block_construct` 对任何以 `>` 开头
    的行本就整格拒绝规整，走不到这里，故不受影响。"""
    i, n = 0, len(line)
    while i < n:
        j, spaces = i, 0
        while j < n and line[j] == " " and spaces < 3:
            j += 1
            spaces += 1
        if j < n and line[j] == ">":
            j += 1
            if j < n and line[j] == " ":       # `>` 后可选一个空格
                j += 1
            i = j
        else:
            break
    return line[i:]


def _is_closing_fence(line: str, fence_char: str, min_len: int) -> bool:
    """闭合围栏：剥掉 blockquote 容器前缀（F4）、stripped 后由「同一字符」构成、
    长度 >= 开口长度。"""
    s = _strip_container_prefix(line).strip()
    return bool(s) and len(s) >= min_len and set(s) == {fence_char}


def _fence_spans(lines: list[str]) -> list[tuple[int, int]]:
    """行式扫描出围栏代码块区间 ``(start, end)``（闭区间、0-based）：``start``
    为开口围栏行，``end`` 为闭合围栏行（未闭合则为最后一行）。开口/闭合遵循
    上面的 run-length 语义——内层 ``` （长度 3）绝不闭合外层 ```` （长度 4）。

    F4：开口/闭合测试前都先剥掉 blockquote 容器前缀（`_strip_container_prefix`），
    故引用块内的围栏也被识别；区间仍以**原始行**下标返回，`_code_blocks`/
    `_tokenize_blocks` 据此取的是带 `>` 前缀的原文（逐字/hash 都含前缀）。"""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        fence_match = _FENCE_OPEN_RE.match(_strip_container_prefix(lines[i]).strip())
        if fence_match:
            fence_char = fence_match.group(1)[0]
            fence_len = len(fence_match.group(1))
            end = n - 1  # 未闭合 -> 跑到输入末尾
            for j in range(i + 1, n):
                if _is_closing_fence(lines[j], fence_char, fence_len):
                    end = j
                    break
            spans.append((i, end))
            i = end + 1
            continue
        i += 1
    return spans


# ---------------------------------------------------------------------------
# 架构性修复：把规整门从 deny-list 翻转成 allow-list（未知结构默认不动）。
#
# 修复前 `is_rich_markdown` 枚举「危险信号」（有围栏 / 有竖线表格 / 有延续行
# / …），任何一行只要不匹配 bullet/ordered/alpha 就被当 prose 逐行重排。可
# markdown 的块级结构是开放集合：四轮独立评审各自发现一种它没学过的结构被打
# 散（围栏代码块、无竖线 GFM 表格、列表延续行，以及 deny-list 至今仍漏的 4
# 空格缩进代码块——`rule_normalize("    def f():\n        return 1")` 会返回
# `"def f():\n\nreturn 1"` 把代码毁掉），而 `content_invariant` 对「文字没变、
# 结构被毁」是结构性失明的，兜不住。与其每发现一种就补一个信号，不如翻转判定
# 方向：只有当**每一行**都命中下面这套封闭文法时才允许规整，否则整格原样返回。
# 门对未知结构 fail CLOSED，是唯一那道防线。
#
# 封闭文法（每行必须是三者之一）：
#   1. 空行（只有空白）；或
#   2. list-marker 行：`classify_line` 判为 bullet/ordered/alpha 的行（复用它，
#      不另写第二套识别器——含 Excel 的 tab/空格缩进 marker，那正是要清理的输入），
#      但其前导空白须过 `_marker_indent_ok` 的 CommonMark 4 空格消歧（含 TAB，或
#      纯空格 <=3）——纯空格 >=4 判为缩进代码块、整格拒绝（见该函数文档）；或
#   3. 顶格 prose 行：**无前导 tab/空格**，且其正文不以任何其它 CommonMark 块级
#      构造开头（围栏 ``` / ~~~、竖线表格 `|`、引用块 `>`、HTML 块 `<` 后接字母/`/`/`!`/`?`、
#      ATX 标题 `#`、引用式链接定义 `[..]:`，或整行只由 `| : - =` 加空白组成且
#      至少含一个 `-`/`=`、长度 >=2——后者一并杀掉 GFM 分隔行、setext 下划线、
#      主题分隔线）。
#
# 关键性质：**任何有前导空白、且本身不是 list marker 的行 -> 整格拒绝**——这
# 一条就通用地关掉了缩进代码块、列表延续行、缩进 prose 这一整类（deny-list 反
# 复漏的正是它）。列 0 以 `**` 开头（如已规整的 `**A. 标题**`，`*` 不跟空白不
# 是 bullet）仍是允许的 prose，保住对已规整格子的幂等。
# ---------------------------------------------------------------------------

_HTML_BLOCK_RE = re.compile(r"^<[A-Za-z/!?]")
_REF_LINK_DEF_RE = re.compile(r"^\[[^\]]*\]:")
_DELIM_LIKE_CHARS = frozenset("|:-= \t")


def _is_delim_like(line: str) -> bool:
    """整行（去两端空白后）只由 `| : - =` 加空白组成、至少含一个 `-`/`=`、
    长度 >= 2。一并覆盖 GFM 表格分隔行（`--- | ---`）、setext 下划线（`===`/
    `---`）、以及 `-`/`=` 构成的主题分隔线——这些行用空行分段都会被腐蚀或
    孤立。纯字母/CJK/数字行（如 `A-B`、`2018`）不落入此集合，仍是允许的 prose。"""
    s = line.strip()
    if len(s) < 2:
        return False
    if not all(ch in _DELIM_LIKE_CHARS for ch in s):
        return False
    return "-" in s or "=" in s


def _starts_block_construct(line: str) -> bool:
    """顶格行（已知无前导 tab/空格）是否以「段落以外的」CommonMark 块级构造
    开头——是则整格拒绝规整。"""
    if _FENCE_OPEN_RE.match(line):                    # 围栏代码块
        return True
    if line[:1] in ("|", ">", "#"):                   # 竖线表格 / 引用块 / ATX 标题
        return True
    if _HTML_BLOCK_RE.match(line):                    # HTML 块（< 后接字母/`/`/`!`/`?`——覆盖标签/闭合标签/注释<!--/PI<?/声明<!/CDATA<![）
        return True
    if _REF_LINK_DEF_RE.match(line):                  # 引用式链接定义 [label]:
        return True
    if _is_delim_like(line):                          # GFM 分隔行 / setext 下划线 / 主题分隔线
        return True
    return False


def _marker_indent_ok(line: str) -> bool:
    """CommonMark 的 4 空格消歧，作用在「已被 `classify_line` 判为 marker」的行上：
    该行的前导空白（只可能是 tab/半角空格）可被门接受，当且仅当

      - 含至少一个 TAB —— 放行（Excel 指纹：这个功能的目标语料全是 tab 缩进，
        对真实生产 notebook 的普查是 40 个 tab、0 行有 >=2 个前导空格），或
      - 纯空格且个数 <= 3 —— 放行（CommonMark：1-3 个前导空格仍是列表项；也正是
        本 emitter 对「有序父项下单层嵌套」发出的缩进——3 个空格）。

    纯空格且个数 >= 4 —— **拒绝**（CommonMark 判为缩进代码块；意图不可辨，fail
    CLOSED）。这堵住了「内容行恰好长得像 marker 的 4 空格缩进代码块」这个洞——
    `classify_line` 只按 `lstrip("\\t ")` 后的形状判 marker，对缩进宽度是盲的。"""
    leading = line[: len(line) - len(line.lstrip("\t "))]
    if "\t" in leading:
        return True
    return len(leading) <= 3


_THEMATIC_BREAK_CHARS = frozenset("*-_")


def _is_thematic_break(line: str) -> bool:
    """CommonMark 主题分隔线：去掉**所有**空格/tab 后由 >=3 个同一字符组成，
    且该字符取自 `* - _`。覆盖 `***`/`___`/`---` 及其空格分隔变体
    `* * *`/`- - -`/`_ _ _`。

    Batch F 规则 2：这类行会被 bullet 正则误判为列表项（`* * *` = `*` + 正文
    `* *` -> 规整成 `- * *`，毁掉水平分隔线），而符号盲的 `content_invariant`
    兜不住。既有 `_is_delim_like` 只认 `| : - =`（裸 `---`），本函数推广到空格
    分隔与 `*`/`_` 形式；两条规则刻意并存、不合并。"""
    stripped = line.replace(" ", "").replace("\t", "")
    if len(stripped) < 3:
        return False
    ch = stripped[0]
    return ch in _THEMATIC_BREAK_CHARS and all(c == ch for c in stripped)


def _has_hard_break(line: str) -> bool:
    """CommonMark 硬换行标记：**非空行**的 RAW 形态以「>=2 个尾随空格」或「单个
    尾随反斜杠」结尾——两者都是段落**内部**的 `<br>`（deliberate hard line break）。

    F1：`_normalize` 会把尾随标记 strip 掉（prose body 走 `.strip()`、marker 行
    的 body 同样 `.strip()`），再把相邻 prose 行用空行分段——于是一次段内硬换行
    被静默改成**段落断裂**，而符号盲的 `content_invariant`（没有任何内容字符变化、
    只有结构变化）兜不住。门必须对它 fail CLOSED、整格拒绝（原样返回）。

    真实 Excel 粘贴的尾随空白几乎总是**单个**空格（对生产语料的普查里唯一的尾随
    空白就是单空格，不是硬换行），仍放行；miss（少规整一格，用户仍可手动触发）
    vs 腐蚀（把内容结构改坏且校验放行）的代价不对称，要求这里拒绝。

    调用方保证只对非空行调用（空行——含只有尾随空格的纯空白行——由 `_line_normalizable`
    的 `blank` 短路在此之前挡掉，不进入本判定）。尾随空格只数半角空格（CommonMark
    硬换行不认 tab），与 TS 孪生 `hasHardBreak` 一致。"""
    if line.endswith("\\"):
        return True
    return len(line) - len(line.rstrip(" ")) >= 2


def _flank_is_space(ch: str) -> bool:
    """强调定界符「侧翼」判据用的空白检测——CommonMark flanking 把行首/行尾也算空白，
    调用方用空串 `""` 表示边界（本函数只判真实字符，边界由调用方短路成「非 open/close」）。
    用 Python `str.isspace()`（含半角空格、TAB、CJK 全角空格 U+3000 等），与 TS 孪生的
    `isPyWhitespace` 逐码点对齐——两侧对「delimiter 两侧是不是空白」结论一致。"""
    return ch.isspace()


def _is_word_flank(ch: str) -> bool:
    """F2：CommonMark「intraword 下划线」判据里的「word 侧」——侧翼是不是字母/数字（含
    CJK）。用 `str.isalnum()`（Unicode 感知：ASCII 字母数字 + CJK 表意 + 各语系字母/数字
    都 True；`_`/`=`/标点/空白/边界空串都 False）。

    仅供 `_has_cross_line_emphasis` 里 **`_` 这一个定界符** 用：CommonMark 规定两侧都是
    word char 的下划线既不能开也不能闭（词内下划线是字面），本扫描器据此整段跳过它（不压
    不弹）。`*`/`~` 不受此约束（它们会词内成对）。刻意**不**把符号 `S*`（`=`/`$`/emoji）
    算 word——只有落在这套 word 判据内才跳过；侧翼是标点/符号时仍走常规 open/close/both
    逻辑（保守方向：宁可多拒不可漏拒一对真正的跨行 `_..._`，如 `(_a\\n b_)`）。

    与 TS 孪生 `isWordFlank`（`/[\\p{L}\\p{N}]/u`）在最终布尔上对齐：两者对 ASCII 字母
    数字、CJK 表意、常见标点/符号一致；侧翼是星际字符时 TS 取到的是代理对半（恒 false），
    Python 见完整码点——星际字母作侧翼在 knowhow 语料里不出现，差分谐波已覆盖所测形状、
    零漂移。"""
    return ch.isalnum()


def _ends_with_open_inline_span(line: str) -> bool:
    """一行是否以**未闭合**的行内构造收尾——即该构造会跨过一个软换行。`_normalize`
    会在相邻 prose 行之间注入空行（软换行 -> 段落断裂）把它拆断，而符号盲的
    `content_invariant`（内容字符没变、只有结构变了）兜不住。门必须对它 fail CLOSED、
    整格拒绝。**独立**追踪三类构造（刻意保守、互不影响；任一在行尾仍开着即拒绝）：

      1. remark-math `$`：按 **run** 配对（`\\$` 跳过——反斜杠转义下一字符）——长度 N 的
         `$`-run 开一个公式 span，只由后面**同长度**的 run 闭合（`$` 行内、`$$` display，
         CommonMark-math 约定；对本门 fail-closed 目的同长度匹配已足够）。行尾仍在开着的
         `$`-run 内 = 未闭合的公式定界符跨行。刻意也拒绝孤立 `价格 $100`（单个开着的 `$`
         run）——对渲染器同样歧义，refuse 只原样留着（miss 而非腐蚀）。display 形态
         `$$x\\n+y$$` 正靠 run 长度识别（旧的逐字符奇偶把 `$$` 当两 `$`＝偶＝闭合而漏掉它）。
      2. 行内代码反引号：按 CommonMark「N 个反引号的 run 由等长 run 闭合，run 内不同
         长度的反引号是字面内容」扫描 run。行尾仍在 span 内 = 未闭合。反引号侧不做
         转义处理（`` \\` `` 仍按裸 run 计——保守，只会多拒不会漏拒）。
      3. 链接/图片文本方括号 `[` `]`：**未转义**的方括号深度（`[` +1、`]` 归零 floor 0）。
         行尾深度 >0 = 有个 `[label` 跨行没闭合（`[label\\ncontinued](url)` 会被空行拆断）。
         只认 ASCII `[]`——全角【】是不同字符、不影响（真实语料正是用【】）。

    强调 `*`/`_` 与 GFM 删除线 `~` 的跨行配对**不在本函数**——它做不到逐行判定：词内
    `R*C`（两侧都是词字符的孤立 run）绝不能因自身就把整格拒掉，但两行各一个 run 若在
    CommonMark/GFM 下**成对**就必须拒。这需要一个跨整格的配对栈，改由
    `_has_cross_line_emphasis`（`*`/`_`/`~` 三个独立栈）在 `is_rich_markdown` 里做一次全格
    扫描（见其文档）。`~` 刻意放在那里而非本函数的 per-line `$`-run 机器里——单个 `~` 就是
    删除线且跨软换行成对，孤立 `~` 却是字面，只有全格栈能区分。

    围栏开口行（```/~~~）由既有 `_starts_block_construct` 在别处拒绝，本函数不为其
    特设分支——即便它也命中「反引号未闭合」，结论一致（都是拒绝）、无害。调用方保证
    只对非空行调用（空行由 `_line_normalizable` 的 blank 短路在此之前挡掉）。与 TS
    孪生 `endsWithOpenInlineSpan` 逐字符对齐（差分谐波零漂移）。"""
    n = len(line)
    # 1) 未转义 `$` 的 run 配对（run 长度相等才闭合；`\$` 转义跳过——镜像下面的反引号处理）。
    #    长度 N 的 `$`-run 开一个公式 span，只由后面**同长度**的 run 闭合（`$` 行内、`$$`
    #    display，CommonMark-math 约定）。行尾仍在开着的 `$`-run 内 -> 未闭合、跨行。旧的
    #    逐字符奇偶把 `$$` 当两个 `$`（偶数＝已闭合）漏掉 display 形态 `$$x\n+y$$`。
    dollar_inside = False
    dollar_open_len = 0
    i = 0
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2                       # 反斜杠转义下一字符（含 \$）——跳过被转义的那个
            continue
        if ch == "$":
            run = 0
            while i < n and line[i] == "$":
                run += 1
                i += 1
            if not dollar_inside:
                dollar_inside, dollar_open_len = True, run
            elif run == dollar_open_len:
                dollar_inside, dollar_open_len = False, 0
            continue                     # run 长度不等 -> span 内字面，保持 inside
        i += 1
    if dollar_inside:
        return True
    # 2) 行内代码反引号 run 配对（run 长度相等才闭合）。
    inside = False
    open_len = 0
    i = 0
    while i < n:
        if line[i] == "`":
            run = 0
            while i < n and line[i] == "`":
                run += 1
                i += 1
            if not inside:
                inside, open_len = True, run
            elif run == open_len:
                inside, open_len = False, 0
            continue                     # run 长度不等 -> span 内字面，保持 inside
        i += 1
    if inside:
        return True
    # 3) 链接/图片文本方括号深度（未转义、floor 0）。
    depth = 0
    i = 0
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2                       # 转义 \[ / \] 既不开也不合深度
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        i += 1
    return depth > 0


def _has_midline_inline_html(line: str) -> bool:
    """F1（review）：一行**任意位置**是否出现「标签形状」的未转义 `<`——即 `<` 紧跟一个
    ASCII 字母或 `/`（`<span`/`</span`/`<em` …）。命中即整格拒绝规整。

    动机：`_starts_block_construct` 的 `_HTML_BLOCK_RE` 只在**行首**认 HTML 块，于是
    `前缀 <span>\\ncontinued</span>` 这类**行中**起头、跨软换行的行内 HTML 漏网——
    `_normalize` 会在相邻 prose 行间注入空行，把空行塞进 `<span>` 内部；而符号盲的
    `content_invariant`（`<`/`>` 都是标点 P、内容字符没变）兜不住这种结构腐蚀。门必须
    对它 fail CLOSED、整格拒绝（原样返回）。

    刻意只认「`<` + 字母/`/`」这一 tag 形状：`a < b`（`<` 后空格）、`x<0`/`i<3`（`<` 后
    数字，是小于号）仍可规整；`x<y`（`<` 后字母）被拒。过度拒绝（miss 一格、用户仍可手动
    触发）与腐蚀（改坏结构且校验放行）的代价不对称，取 fail-closed、接受偶尔多拒。`\\<`
    转义的 `<`（反斜杠转义下一字符）不算标签起头。与 TS 孪生 `hasMidlineInlineHtml` 逐
    字符对齐。调用方保证只对非空行调用（空行由 `_line_normalizable` 的 blank 短路挡掉）。"""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2                       # 反斜杠转义下一字符：\< 不是标签起头
            continue
        if ch == "<":
            nxt = line[i + 1] if i + 1 < n else ""
            if nxt == "/" or (nxt.isascii() and nxt.isalpha()):
                return True
        i += 1
    return False


def _has_cross_line_emphasis(lines: "list[str]") -> bool:
    """全格（cell-global）强调配对：`*`/`_`/`~` 定界符 run 的 CommonMark-lite 开合，用
    **三个跨整格的栈——每个定界符字符各一个独立栈**（每个 entry 记它所在的行号）判定
    是否有一对强调**跨两个不同的行**成对。成对且跨行 -> 返回 True（`is_rich_markdown`
    据此整格拒绝）。

    F2（栈按分隔符字符分型）：`*`-run 只与 `*` 栈交互、`_`-run 只与 `_` 栈交互、`~`-run
    只与 `~` 栈交互。旧的单一无类型栈会让词内 `_`（BOTH run）弹掉一个 `*` opener
    （CommonMark 绝不把 `*` 与 `_` 配对）——于是 `*open R_C\\nclose*` 里真正的 `*...*`
    跨行对被 `_` 偷走、检不出，cell 放行、corruption 存活。独立栈彻底规避定界符交错，
    比单栈严格更精确。

    F1（review，GFM 删除线 `~`）：`~` 加入本函数（**不**在 `_ends_with_open_inline_span`
    的 per-line `$`-run 机器里）。remark-gfm 4.0.1（前端渲染器，`remarkGfm` 默认选项）里
    **单个 `~` 就是删除线**、且会**跨软换行**成对（node repl 实测：`~a\\nb~`→`<del>`
    "a\\nb"、`~5ns\\n延迟~`→`<del>`、`foo~bar\\nbaz~qux`→`<del>` "bar\\nbaz"；而孤立的
    `约~5ns`/`foo~bar`→无 `<del>`，`~a\\n\\nb~` 空行断开→无 `<del>`）。per-line 追踪无法
    区分「孤立 `约~5ns`（须规整）」与「跨行成对 `~5ns\\n延迟~`（须拒）」——只有跨整格配对
    栈能（孤立 run 压栈成 leftover 不拒、跨行成对才拒）。`~` 用与 `*` **相同**的 BOTH 语义
    （词内也活跃），**不**走下面 `_` 的词内字面规则——因为实测词内 `~` 会跨行成对。
    行首 `~~~+` 围栏由 `_starts_block_construct` 在别处整格拒绝，本函数不特设分支。

    F2（review，词内 `_` 字面）：CommonMark 规定两侧都是 word char 的下划线既不能开也
    不能闭（词内下划线是字面，**不同于** `*`）。`_outer foo_bar\\nclose_` 里真正的
    opener 是行首 `_`、closer 是行尾 `_`，二者跨软换行成对；`foo_bar` 中间那个 `_` 是词内
    字面。旧 BOTH 分支把词内 `_` 当真 closer 弹掉了行首那个真 opener，于是真正的跨行
    `_..._` 对没被登记、cell 放行、注入空行拆断强调、符号盲 content_invariant 兜不住。
    修复：**仅 `_` run** 两侧都过 `_is_word_flank` 时整段跳过（不压不弹）；`*`/`~` 保持
    BOTH 语义（它们会词内成对）。侧翼是标点/符号（非 word）的 `_` 仍走常规 open/close/both
    （保守，兜住 `(_a\\n b_)` 这类标点侧翼的真跨行对）。

    为什么必须全格、而非逐行（F1 修复动机）：词内乘法 `R*C`（两侧都是词字符）是一个
    **BOTH** run，逐行看它「既能开也能闭」，旧实现判它中性、放行——对孤立 `R*C` 是对的
    （真实语料的承重用例，必须仍可规整）。但 `这是*跨行\\n强调*内容` 的两个 `*` 同样都
    是 BOTH run，CommonMark 会把它们**跨软换行**配成一对强调；`_normalize` 注入空行会把
    这对强调拆断，符号盲的 `content_invariant`（`*` 被当标点 P 剥掉、内容字符没变）兜不住。
    逐行中性规则对这两种形状无法区分，只有「跨整格配对、看成对的两端是否落在不同行」才能
    既救 `R*C`、又拒 `这是*跨行\\n强调*内容`。

    F1（本批，按分隔符**个数**配对）：run 不再当作一个整体压/弹一次——长度 N 的 run
    携带 N 个定界符 unit。旧实现把一整个 run 只弹一次：`*outer\\n*inner**` 里闭合 `**`
    （长度 2）只弹掉行 1 的 opener，行 0 的 opener 悬空、跨行对没登记、cell 放行 ->
    `_normalize` 注入空行把强调拆断，符号盲的 `content_invariant` 兜不住。改为按 unit
    记账后闭合 run 会逐个弹、每个 unit 各成一对，行 0 的那对被识别为跨行 -> 拒绝。

    每个 run 按 CommonMark-lite 分类（行首/行尾算空白），按 unit（run 长度 N）作用：
      - CAN-OPEN-only（右邻非空白、左邻空白）-> 压入 N 个 unit（各记当前行号）；
      - CAN-CLOSE-only（左邻非空白、右邻空白）-> 弹出至多 N 个 unit，**每个**成一对；
      - BOTH（两侧都是词字符，词内形状）-> 栈非空则弹出至多 N 个 unit（各成一对），
        否则压入 N 个 unit。
    每弹出一个 unit 即比较它与当前行号：不同 -> 跨行强调 -> 返回 True（任一对跨行即拒）。

    **承重的记账细节**：逐行、按 run 在行内出现的先后，依次作用到**该字符对应的**全格栈上。
    于是一行内自身成对的 `**bold**`（开 run 压栈、紧接的闭 run 弹掉的正是它自己）不会去
    偷更早一行留在栈底的悬空 opener——`…R*C\\n\\n**参考信息**\\n正文` 里 `R*C` 压栈后一直
    留在栈底，`**参考信息**` 的开/闭 run 自成一对（同一行），`R*C` 的 entry 纹丝不动、最终
    作为**未配对的 leftover** 留在栈里。**未配对的 leftover 自身不构成拒绝**——这正是救
    `R*C` 的关键（孤立 run 压栈、永不成对），也让一个永不成对的悬空 opener（`first *middle`
    后再无 closer）不再被误拒（它在 CommonMark 下不构成强调，注入空行拆不断不存在的 span）。
    `\\*`/`\\_` 转义定界符跳过。

    与 TS 孪生 `hasCrossLineEmphasis` 逐字符对齐。"""
    star_stack: "list[int]" = []       # `*` 未配对 opener 的行号
    under_stack: "list[int]" = []      # `_` 未配对 opener 的行号
    tilde_stack: "list[int]" = []      # `~` 未配对 opener 的行号（三个定界符字符各一个独立栈）
    for lineno, line in enumerate(lines):
        n = len(line)
        i = 0
        while i < n:
            ch = line[i]
            if ch == "\\":
                i += 2                   # 转义 \* / \_ / \~ 不是定界符
                continue
            if ch == "*" or ch == "_" or ch == "~":
                run_char = ch
                if run_char == "*":                 # F2：按分隔符字符分型（三个独立栈）
                    stack = star_stack
                elif run_char == "_":
                    stack = under_stack
                else:
                    stack = tilde_stack
                start = i
                while i < n and line[i] == run_char:
                    i += 1
                run_len = i - start                 # F1（本批）：run 携带的定界符**个数**
                prev_ch = line[start - 1] if start > 0 else ""
                next_ch = line[i] if i < n else ""
                # F2：词内 `_`（且仅 `_`）—— 两侧都是 word char 时既不开也不闭，整段跳过。
                if run_char == "_" and _is_word_flank(prev_ch) and _is_word_flank(next_ch):
                    continue
                can_open = bool(next_ch) and not _flank_is_space(next_ch)
                can_close = bool(prev_ch) and not _flank_is_space(prev_ch)
                if can_open and can_close:          # BOTH（词内形状；`*`/`~` 会成对）
                    if stack:
                        # 栈非空 -> 弹出至多 run_len 个 unit（每个 unit 成一对）；否则整段压栈
                        for _ in range(min(run_len, len(stack))):
                            if stack.pop() != lineno:
                                return True
                    else:
                        stack.extend([lineno] * run_len)
                elif can_open:                       # CAN-OPEN-only：压 run_len 个 unit
                    stack.extend([lineno] * run_len)
                elif can_close:                      # CAN-CLOSE-only：弹至多 run_len 个 unit，逐个成对
                    for _ in range(min(run_len, len(stack))):
                        if stack.pop() != lineno:
                            return True
                continue
            i += 1
    return False


def _line_normalizable(line: str, info: LineInfo) -> bool:
    """封闭文法：一行是否落在「可规整」的允许集合内（见上方文档）。`info` 由
    调用方 `is_rich_markdown` 预先 `classify_line` 传入（同一行只分类一次，也供
    其跨行的懒延续判定复用）。"""
    if info.kind == "blank":
        return True                                   # 规则 1：空行
    # F1：CommonMark 硬换行（>=2 尾随空格 / 单个尾随反斜杠）在**任何**非空行上都
    # 必须整格拒绝——规整会 strip 掉标记并用空行把段内换行改成段落断裂（见
    # `_has_hard_break` 文档）。放在 marker/prose 分派之前，故对 list-marker 行
    # （body 会被 classify_line 的 `.strip()` 悄悄吞掉尾随硬换行）同样生效。
    if _has_hard_break(line):
        return False
    # Batch F 规则 2：CommonMark 主题分隔线必须在 marker 接受**之前**拦截——
    # `* * *`/`- - -`/`_ _ _`/`***`/`___` 会被当成 list marker 规整、毁掉分隔线。
    if _is_thematic_break(line):
        return False
    # 行尾仍开着的行内构造（未闭合的 `$` 公式 / 反引号代码 / `[` 链接·图片文本）会跨
    # 软换行——`_normalize` 注入空行会把它拆断，符号盲的 content_invariant 兜不住 ->
    # 整格拒绝。放在 marker/prose 分派之前，故对 list-marker 行的正文同样生效。强调
    # `*`/`_` 的跨行配对改由 `_has_cross_line_emphasis` 在 `is_rich_markdown` 做全格判定。
    if _ends_with_open_inline_span(line):
        return False
    # F1（review）：行中「标签形状」的未转义 `<`（`<span`/`</em` …）会跨软换行——
    # `_normalize` 注入空行把它拆断、符号盲的 content_invariant 兜不住 -> 整格拒绝。
    # 放在 marker/prose 分派之前，故对 list-marker 行的正文同样生效。
    if _has_midline_inline_html(line):
        return False
    if info.kind in ("bullet", "ordered", "alpha"):
        return _marker_indent_ok(line)                # 规则 2：list-marker 行（含 CommonMark 4 空格消歧）
    # 规则 3：顶格 prose——有前导 tab/空格的非 marker 行一律拒绝（缩进代码块 /
    # 列表延续行 / 缩进 prose 这一整类），顶格但以其它块级构造开头的也拒绝。
    if line[:1] in (" ", "\t"):
        return False
    return not _starts_block_construct(line)


def is_rich_markdown(raw: str) -> bool:
    """是否「不安全规整」——allow-list 门（deny-list 的语义反转，保留原导出名以
    减少改动面）：当且仅当**每一行**都命中 `_line_normalizable` 的封闭文法、且不
    含 Batch F 规则 1 的懒延续相邻时返回 False（可规整）；只要有一行落在文法之外
    （或触发懒延续），就返回 True（原样不动）。对未知结构 fail CLOSED——完整文法
    与动机见上方注释。

    Batch F 规则 1（上下文感知）：顶格 prose 行**紧跟**（无空行）一个 marker 行
    （bullet/ordered/alpha）时，是该列表项的 CommonMark 懒延续，`_normalize` 会
    注入空行把它拆下来、`content_invariant` 字符盲兜不住 -> 整格拒绝。marker 与
    prose 间夹一个空行会按 CommonMark 断开延续，故 marker -> 空行 -> prose 仍放行
    （已规整语料的常见形状——列表后隔空行接下一段散文）。

    F1 强调/删除线跨行配对（全格）：`_has_cross_line_emphasis` 先做一次跨整格的
    `*`/`_`/`~` 配对扫描——任一对强调或删除线落在两个不同行即拒绝（注入空行会拆断它，
    符号盲兜不住）；孤立的词内 `R*C`/`foo~bar`（永不成对）仍放行（见该函数文档）。

    空/纯空白输入返回 False（交给 `_normalize` 归一为空串）。"""
    if not raw or not raw.strip():
        return False
    src = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    if _has_cross_line_emphasis(lines):
        return True              # F1：强调跨行成对，注入空行会拆断
    prev_kind = "blank"          # 首行无前驱：非 marker，规则 1 不触发
    for line in lines:
        info = classify_line(line)
        if not _line_normalizable(line, info):
            return True
        if info.kind == "prose" and prev_kind in ("bullet", "ordered", "alpha"):
            return True          # 规则 1：marker 后紧跟顶格 prose = 懒延续
        prev_kind = info.kind
    return False


def rule_normalize(raw: str) -> str:
    """规整入口——永不抛：任何异常返回原文。

    先过 `is_rich_markdown` 这道保守闸门：命中就直接原样返回 `raw`（连
    CRLF 归一化、`.strip()` 都不做——「完全不动」是字面意思），完全不进入
    `_normalize` 的逐行分类/重排。"""
    try:
        if is_rich_markdown(raw):
            return raw
        return _normalize(raw)
    except Exception:
        return raw


def safe_rule_normalize(raw: str) -> "tuple[str, bool]":
    """C1 layer 2（纵深防御）：``rule_normalize`` 规整 + 用
    ``content_invariant`` 兜底校验。规整结果理论上必须只改格式（这正是
    ``rule_normalize`` 的契约），但万一它出 bug 改坏了内容（C1 那类块级结构
    被拆散正是活生生的例子——校验闸门本来就存在，只是从未接到规则路径上），
    这里绝不让改坏的候选流向调用方：返回原文，并告知调用方候选是否被采用。

    返回 ``(result, used_rule)``——``used_rule=False`` 时 ``result`` 就是
    ``raw`` 本身（字节级不变）；调用方（导入/追加落库路径、存量回填脚本）
    据此決定是否要在自己的展示/报告里打一个独立于普通 "rule" 的来源标签。
    """
    candidate = rule_normalize(raw)
    if content_invariant(raw, candidate):
        return candidate, True
    return raw, False


def _normalize(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    src = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")

    # `is_rich_markdown` 这道 allow-list 门已保证：能走到 `_normalize` 的 cell，
    # 每一行都是空行 / list-marker 行 / 顶格 prose——没有围栏、表格、缩进代码块
    # 之类需要「原样透传」的块级结构（那些会在门口被整格拒绝，`rule_normalize`
    # 直接原样返回）。所以这里纯逐行分类/重排，不再有 verbatim 区间与保护掩码。
    infos = [classify_line(line) for line in lines]
    non_blank = [info for info in infos if info.kind != "blank"]
    cell_min = min((info.depth for info in non_blank), default=0)

    out: list[str] = []
    prev = "start"              # 'prose' | 'list' | 'header' | 'start'
    group_base: "int | None" = None    # 当前列表组是否已开始（非 None = 组内）；具体层级判断见 depth_stack
    # P2 修复：每个已打开层级的「源 depth」+ 实际发出的 marker 宽度（"- " ->
    # 2，"1. " -> 3，"10. " -> 4）。子项的缩进 = 其所有祖先层级宽度之和，而
    # 不是固定「每层 2 格」——CommonMark 要求子块缩进到父项 marker 之后内容
    # 开始的那一列；父项是 bullet/alpha（恒渲染成 "- "，宽度 2）时两者恰好
    # 相等，但父项是 ordered 时 marker 宽度随数字位数变化，固定 2 格会让
    # "1. parent\n  - child" 解析成两个并列的顶层列表，而不是嵌套（用真实
    # remark/GFM 解析器验证过）。
    #
    # 层级判断刻意用「栈 + 序数比较」（比当前栈顶更深 / 持平 / 更浅），不用
    # `info.depth - group_base` 减法：`_indent_depth`（tab 数 + 前导空格数
    # // 2）对「非 2 的倍数」的缩进宽度是有损的——比如宽度 4（双位数 ordered
    # 的子项）反解出来的 depth 是 2 而不是 1，减法会把它错判成多一层，规整
    # 结果自身再规整一遍（幂等性）就会一次比一次多缩进（已用双位数 ordered /
    # 三层全 ordered 嵌套两个场景验证过这个漂移）。栈序数比较只关心「比当前
    # 已打开的最深层级更深 / 一样 / 更浅」这个相对关系，而 `_indent_depth`
    # 对纯空格前缀始终单调不减（缩进越多，反解出的 depth 只增不减，不会
    # 出现「更深的缩进反解出更小的 depth」这种顺序颠倒），所以序数比较在这种
    # 有损换算下仍然稳定、可幂等。随 group_base 一起重置——新列表组不继承
    # 上一组的祖先层级。
    depth_stack: "list[int]" = []      # 每个已打开层级对应的源 depth，按层级顺序（浅→深）
    level_width: "list[int]" = []      # 与 depth_stack 一一对应：该层级目前的 marker 渲染宽度

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
                group_base = info.depth          # 新列表组已开始（关键：随 prose/header 重置）
                depth_stack, level_width = [], []
            while depth_stack and depth_stack[-1] > info.depth:
                depth_stack.pop()                # 回到更浅的层级：先弹出已经更深的祖先
                level_width.pop()
            if depth_stack and depth_stack[-1] == info.depth:
                level = len(depth_stack) - 1     # 命中已打开的同一层级 -> 该层的兄弟项
            else:
                level = len(depth_stack)          # 比当前已打开的最深层级更深 -> 开一个新层级
                depth_stack.append(info.depth)
                level_width.append(0)             # 占位，下面立即写入真实宽度
            marker = f"{info.marker}. " if info.kind == "ordered" else "- "
            indent = " " * sum(level_width[:level])   # 缩进 = 祖先层级（不含自己）宽度之和
            level_width[level] = len(marker)           # 这一层「当前」宽度，供更深子项引用
            if prev in ("prose", "header"):
                out.append("")                   # 列表开始前补空行
            out.append(f"{indent}{marker}{info.body}")
            prev = "list"
        else:  # prose
            if out:
                out.append("")                   # 相邻散行各自成段
            out.append(info.body)
            prev, group_base = "prose", None

    # 折叠连续空行（沿用旧版 `\n{3,}` 折叠的用意）。门反转后 `_normalize` 只处理
    # 逐行 prose/marker，本身不会发出连续空行，这一步实际是防御性 no-op，保留以
    # 兜住未来的重排改动。
    collapsed: list[str] = []
    j, m = 0, len(out)
    while j < m:
        if out[j] == "":
            collapsed.append("")
            k = j + 1
            while k < m and out[k] == "":
                k += 1
            j = k
            continue
        collapsed.append(out[j])
        j += 1

    result = "\n".join(collapsed).strip()
    return result


# ---------------------------------------------------------------------------
# 内容不变式校验（零 LLM）：保证 LLM 重排只改格式、不改内容。
# ---------------------------------------------------------------------------

_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")
# F3：自动链接 `<scheme:...>`——scheme 首字母 + 1..31 个 `[A-Za-z0-9+.-]`（CommonMark 的
# 2..32 字符 scheme）+ `:` + 到 `>` 之间无空白/`<`/`>`。要求 `:` 故 HTML 标签 `<div>`
# （无冒号）不误匹配；`\s` 含换行故自动链接不跨行。
_AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*>")

# F1（review，GFM 裸自动链接**字面**）：CommonMark-GFM 的 autolink 扩展（前端渲染器
# remark-gfm 默认开）会把散文里**没有**尖括号的裸 URL/邮箱也渲染成可点 `<a>`——
# `https://host/a-b`、`www.example.com`、`user.name@example.com` 皆是。旧扫描器只认尖
# 括号 autolink `<...>`，于是这些裸字面落进「标点被放宽」的散文区，`https://host/a-b`→
# `https://host/ab` 这种只动 URL 标点的改写蒙混过签名与逐字比对。现在把裸字面也纳入
# 与尖括号 autolink 同级的逐字保护（同归 kind="autolink" -> 共享 "URL" token 前缀 +
# `_link_refs` 的逐字比对）。
#
# 匹配（见 `_match_bare_autolink`）：URL 前缀 `http://` `https://` `www.`（**词边界**
# 起始）+ 贪婪吃到空白/`<`，再按 GFM autolink_delim 剥尾随标点（见 `_trim_gfm_url_end`）；
# 邮箱按任务给定的简单文法 `[alnum._+-]+@[alnum-]+(\.[alnum-]+)+`。有意偏离 cmark-gfm
# 并记录在案的简化：
#   · 起始判据 = 「前一字符非字母数字」（词边界），比 cmark-gfm 的「行首/空白/`*_~(`」
#     略宽——更宽只会**多**保护 URL，对本不变式（保护 URL 不被悄改）是安全方向；
#   · 尾随剥离额外剥掉**非 ASCII 的 Unicode 标点**（中文句读 。，、）！？ 等）——
#     cmark-gfm 只剥 ASCII，但中文散文里 URL 后的句读是句子标点、不是 URL 的一部分，
#     纳入剥离让「URL 后标点归一化」这类合法改写照常通过（见测试）。
# 与已有的数字语境标点规则（`_is_numeric_context_punct`）**互补不冲突**：tokenize 先跑，
# URL 内的标点随整段收进不透明 token，数字语境规则只作用在 token 之外的散文数字上。
# Python-only（本模块的不变式无 TS 孪生）。
_GFM_BARE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<]+", re.IGNORECASE)
_GFM_EMAIL_RE = re.compile(r"[A-Za-z0-9._+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_GFM_URL_TRAILING_PUNCT = frozenset("?!.,:;*_~'\"")


def _trim_gfm_url_end(url: str) -> int:
    """裸 URL 主体（调用方保证已由 `[^\\s<]+` 截得、内部无空白/`<`）的 GFM 尾随标点
    剥离，返回算作 autolink 目的地的**字节长度**（该长度之后的尾字符算散文、不算 URL）。
    反复从尾部剥，直到尾字符不再是「尾随标点」：

      · `?!.,:;*_~'"`（cmark-gfm autolink_delim 的 ASCII 尾随集）——剥；
      · `)`——仅当整段里 `)` 多于 `(`（失衡）才剥那个尾随 `)`；平衡/多开时保留
        （`http://a(b)` 尾部的 `)` 是 URL 的一部分）；
      · `;`——尾部像 HTML 实体引用（`&…;`）时连引用一起剥，否则保留；
      · 非 ASCII 的 Unicode 标点 `P*`（中文句读 。，、）！？ 等）——剥（记录在案的简化，
        见上方注释块）。"""
    end = len(url)
    while end > 0:
        c = url[end - 1]
        if c in _GFM_URL_TRAILING_PUNCT:
            end -= 1
        elif c == ")":
            if url.count(")", 0, end) <= url.count("(", 0, end):
                break
            end -= 1
        elif c == ";":
            j = end - 1
            while j > 0 and url[j - 1].isalnum():
                j -= 1
            if j > 0 and url[j - 1] == "&":
                end = j - 1                # 疑似实体引用 `&…;` -> 连 `&` 一起剥
            else:
                break
        elif not c.isascii() and unicodedata.category(c).startswith("P"):
            end -= 1
        else:
            break
    return end


def _match_bare_autolink(text: str, pos: int) -> "int | None":
    """裸 GFM autolink 字面（URL / www / 邮箱）从 `pos` 起的匹配：命中返回构造之后一位的
    下标（半开 end），否则 ``None``。要求 `pos` 处于**词边界**——`pos` 前一字符非字母数字
    （行首/空白/标点/CJK 句读/`_`/`*`/`(` 等都算边界；紧跟在字母数字后的 `http`/`www` 是
    更长单词/标识符的一部分、不匹配）。URL 先于邮箱尝试（`http://user@host` 是带 userinfo
    的 URL，不是邮箱）。URL 命中后 `_trim_gfm_url_end` 剥尾随标点；剥完只剩裸 scheme 前缀
    （`https://.` -> `https://`、`www.` -> `www`）判为退化、不算构造（返回 None）。见上方
    `_GFM_BARE_URL_RE` 注释块。"""
    prev = text[pos - 1] if pos > 0 else ""
    if prev.isalnum():
        return None                        # 非词边界：更长单词/标识符的一部分
    m = _GFM_BARE_URL_RE.match(text, pos)
    if m:
        matched = m.group(0)
        low = matched.lower()
        prefix_len = 8 if low.startswith("https://") else 7 if low.startswith("http://") else 4  # "www."
        trimmed_len = _trim_gfm_url_end(matched)
        if trimmed_len <= prefix_len:
            return None                    # 剥完只剩裸 scheme 前缀 -> 退化，不算构造
        return pos + trimmed_len
    m = _GFM_EMAIL_RE.match(text, pos)
    if m:
        return m.end()
    return None


def _scan_balanced(text: str, i: int, n: int, open_ch: str, close_ch: str) -> "int | None":
    """从 ``i``（开口字符之后一位）起按深度消费平衡的 ``open_ch``/``close_ch`` 对（起始
    深度 1，`\\` 转义下一字符——既不开也不合深度）。返回闭合字符之后一位；未闭合（扫到
    文本末尾深度仍 >0）返回 ``None``。图片/链接的 alt（`[`/`]`）与 dest（`(`/`)`）共用它——
    「宁可漏认、不可乱切」。"""
    depth = 1
    while i < n and depth > 0:
        ch = text[i]
        if ch == "\\":
            i += 2                # 反斜杠转义下一字符：既不闭合也不开启深度
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
        i += 1
    return i if depth == 0 else None


def _find_link_constructs(text: str) -> "list[tuple[int, int, str]]":
    """模块内**唯一**的「链接式」构造扫描器：一趟从左到右扫出所有 CommonMark 图片
    `![alt](dest)`、行内链接 `[text](dest)`、尖括号自动链接 `<scheme:...>`，**以及**
    （F1 review）GFM 裸自动链接字面 `https://…`/`www.…`/邮箱，返回区间 + 类别
    ``(start, end, kind)``（半开，``end`` 指向构造之后一位；``kind`` ∈
    {"image","link","autolink"}——裸字面归入 "autolink"，与尖括号 autolink 共享
    "URL" token 前缀与 `_link_refs` 逐字比对）。`_find_images`（filter image）、
    `_link_refs`（filter link/autolink）、签名的原地 token 化都复用它——评审要求
    「一份 matcher、别两处漂移」，并据此把「链接目的地」纳入与图片同级的逐字保护。

    F3（review）：旧实现只认图片，**行内链接/自动链接**落进「标点被放宽」的散文区，于是
    `[docs](https://host/a-b)`→`(https://host/ab)` 这种只动 URL 标点的改写蒙混过 signature
    与逐字比对。现在链接/自动链接和图片一样进逐字 ref 比对 + 原地 token 化。

    分类靠 `!` 前缀（满足「一个扫描器按 `!` 前缀分型」）：`[` 前一字符是 `!` -> image，
    否则 link。alt/text 用平衡方括号、dest 用平衡圆括号（都 `\\` 转义感知，`_scan_balanced`
    同一套——`![diagram[final]](u)` 的嵌套 `]`、`![x](a/b_(c).png)` 的内层 `()`、`![x](foo\\)bar)`
    的转义 `)` 都完整捕获；未闭合则**不算**构造，按普通文本处理，宁可漏认不可乱切）。`[text]`
    后不紧跟 `(dest)` 的不算行内链接——引用式定义 `[label]: dest` 是**行**构造（且规则门
    `_starts_block_construct` 的 `_REF_LINK_DEF_RE` 已整格拒绝、LLM 输入里罕见），本字符
    扫描器刻意不解析它（见 `content_invariant` 注释）。自动链接要求 scheme >=2 字符 + `:`，
    故 HTML 标签 `<div>`（无冒号）不误匹配、`<...>` 内 `\\s` 含换行故不跨行。

    顶层 `\\` 转义跳过下一字符（`\\[`/`\\<` 不开启链接/自动链接）；游标推进跳过已消费区间、
    避免重叠误匹配（下一处开口可能落在已消费的 dest 内部）。"""
    spans: "list[tuple[int, int, str]]" = []
    pos, n = 0, len(text)
    while pos < n:
        ch = text[pos]
        if ch == "\\":
            pos += 2                          # 顶层反斜杠转义：\[ / \< 不开启构造
            continue
        if ch == "[":
            is_image = pos > 0 and text[pos - 1] == "!"
            start = pos - 1 if is_image else pos
            after_alt = _scan_balanced(text, pos + 1, n, "[", "]")     # alt/text 平衡方括号
            if after_alt is None or after_alt >= n or text[after_alt] != "(":
                pos += 1                       # alt 未闭合 / 其后不是 `](`：不是链接/图片
                continue
            after_dest = _scan_balanced(text, after_alt + 1, n, "(", ")")  # dest 平衡圆括号
            if after_dest is None:
                pos += 1                       # 目的地未闭合：不算构造
                continue
            spans.append((start, after_dest, "image" if is_image else "link"))
            pos = after_dest
            continue
        if ch == "<":
            m = _AUTOLINK_RE.match(text, pos)
            if m:
                spans.append((pos, m.end(), "autolink"))
                pos = m.end()
                continue
        # F1（review）：GFM 裸 autolink 字面（`https://…` / `www.…` / 邮箱）。行内链接/
        # 图片/尖括号 autolink **先扫**——命中它们时游标已 `pos = after_dest/m.end()` 跳过
        # 整段，故这里的裸扫描只落在「剩余散文」上，绝不会把已在 `[text](url)` 内的 URL 二次
        # 匹配（满足评审的「inline-link scan first, bare-literal scan on the remainder」）。
        bare_end = _match_bare_autolink(text, pos)
        if bare_end is not None:
            spans.append((pos, bare_end, "autolink"))
            pos = bare_end
            continue
        pos += 1
    return spans


def _find_images(text: str) -> "list[tuple[int, int]]":
    """所有 CommonMark 图片构造 `![alt](dest)` 的区间 ``(start, end)``（半开）——
    `_find_link_constructs` 的 image 过滤视图（既有调用方 `_image_refs`/签名 token 化的
    图片语义完全不变）。"""
    return [(start, end) for (start, end, kind) in _find_link_constructs(text) if kind == "image"]


def _code_block_spans(md: str) -> "tuple[list[str], list[tuple[int, int]]]":
    """把 ``md`` 切成行、用模块唯一的行式围栏扫描器 `_fence_spans` 找出围栏代码
    块区间，返回 ``(lines, spans)``。签名与逐字校验都走这一份——不再用 DOTALL
    正则 `` ```.*?``` `` 就近配对（那会把内含 ``` 示例的 4 反引号块错切成碎片，
    块内改动对签名隐形，正是 P1 之后仍存在的假通过根因）。"""
    lines = (md or "").split("\n")
    return lines, _fence_spans(lines)


def _inline_code_spans_in_line(line: str) -> "list[tuple[int, int]]":
    """F2（review）：**逐行**扫出 CommonMark 行内代码 span 的区间 ``(start, end)``（半开、
    含定界反引号）。长度 N 的反引号 run 开启一个 span，由后面**最近的等长 run** 闭合；
    span 内长度不等的反引号是字面内容。未闭合的开口 run 不算 span（其后字符照常处理）。

    **逐行**（不跨行）是刻意的：跨软换行的行内代码在规则门（`_ends_with_open_inline_span`）
    处已整格拒绝、走不到签名重排；在 LLM 输入里，未闭合的反引号就当字面反引号处理（随后
    被 `content_signature` 的 `_EMPHASIS_RE` 剥掉），与旧行为一致。调用方（`_tokenize_blocks`/
    `_inline_code_refs`）保证围栏代码块的行**先被排除**，故这里只作用在散文行上。"""
    spans: "list[tuple[int, int]]" = []
    i, n = 0, len(line)
    while i < n:
        if line[i] == "`":
            start = i
            run = 0
            while i < n and line[i] == "`":
                run += 1
                i += 1
            # 找**等长**闭合 run（长度不等的反引号是 span 内字面内容）。
            j = i
            closed: "int | None" = None
            while j < n:
                if line[j] == "`":
                    k = j
                    r2 = 0
                    while k < n and line[k] == "`":
                        r2 += 1
                        k += 1
                    if r2 == run:
                        closed = k
                        break
                    j = k                # 长度不等 -> 跳过整段、继续找
                else:
                    j += 1
            if closed is not None:
                spans.append((start, closed))
                i = closed
            # 未闭合：i 已越过开口 run，继续扫余下字符
        else:
            i += 1
    return spans


def _is_content_char(ch: str) -> bool:
    """内容 = 字母/数字/CJK（`L*`/`N*`）+ 所有 Unicode 符号类 `S*`（`Sm` 数学、
    `So` 其它/emoji、`Sc` 货币、`Sk` 修饰符）+ **所有组合标记类 `M*`**（`Mn` 非
    间距、`Mc` 间距组合、`Me` 包围）。**放宽（不计入内容）的只有：空白 + 所有标点类
    `P*`**（另加 content_signature 里先做的 marker/强调/标题符剥离）。

    改动动机（F2）：旧实现只留 alnum/CJK，于是把「语义符号」也当标点放宽了——`V=IR`→
    `V+IR`、`x<0`→`x>0`、`✅`→`❌` 两侧字母/CJK 不变，签名相等、被当「只改格式」放行，
    实则语义被改。把符号 `S*` 归为内容后这些改动会被正确拒绝，而用户拍板的标点放宽
    （`R：`→`R:`、`（1W1S）`→`(1W1S)`、`、`→`,`）仍完好——它们都是 `P*` 类。

    组合标记 `M*` 同样是内容（本批修复）：旧实现把 `M*` 也剥掉，于是**分解形**的
    `café`（`e` + U+0301 组合尖音符 Mn）与 `cafe` 签名相等、被当「只改格式」放行；印
    度文的元音符号（matra，如 U+093E 间距组合标记 Mc）被删也检测不到；emoji 的变体
    选择符（VS16 U+FE0F，Mn）丢失同样隐形——全是被误当「纯格式」的有意义字符。

    注意：本不变式比较**原始码点**、不做 Unicode 归一化，故 NFC 的 `é`（U+00E9，字母
    Ll）与 NFD 的 `é`（`e` + U+0301）码点序列不同 -> `content_invariant` 判 False。这是
    **fail-closed**（LLM 改了 Unicode 归一化形式会被拒、退回确定性规则路径），是有意的语义。

    易混淆字符的类别（`unicodedata.category` 核对；`S*`/`M*`=内容，`P*`=放宽）：
      =  Sm   +  Sm   <  Sm   >  Sm   →  Sm        ← 数学符号：内容
      ✅ So   ❌ So   ￥ Sc   $  Sc                ← emoji/货币：内容
      U+0301 Mn   U+FE0F(VS16) Mn   U+093E(matra) Mc  ← 组合标记：内容
      *  Po   \\  Po   ：Po   、Po   （Ps  ）Pe     ← 标点：放宽
    注：`*`/`_`/反引号还会被 `content_signature` 的 `_EMPHASIS_RE` 在字符过滤**之前**
    先剥掉（它们是强调 marker），故 `S*` 规则不会误把强调符提升为内容。"""
    if ch.isascii() and ch.isalnum():
        return True
    cat = unicodedata.category(ch)          # 'Lo' = CJK 等表意文字；'Nd'/'Nl' = 数字；'S*' = 符号；'M*' = 组合标记
    return cat[0] in ("L", "N", "S", "M")


def _is_numeric_context_punct(body: str, i: int) -> bool:
    """标点在**数字语境**里是内容（不放宽）——`_is_content_char` 是无上下文的逐字
    判定，把所有 `P*` 一律放宽；但工程数据里的小数点、千分位、斜杠比值、一元负号、
    百分号、比值冒号都承载语义，剥掉它们会让 `-1.5 V`≡`15 V`、`版本 1.2.3`≡
    `版本 123`、`3:1`≡`31`、`1,000`≡`1000`、`50%`≡`50` 这些语义改写被误判成「只改
    格式」放行（F2）。故 `content_signature` 的字符过滤在 `_is_content_char` 之外，
    再按**相邻码点**把下列 ASCII 标点当内容留下：

      `.` `,` `/` `:`  左右两侧都是十进制数字（Nd）：`1.5` `1,000` `3/4` `3:1`
      `-`              其右紧邻是数字、且其左紧邻不是字母数字（一元负号/负数）：`-1.5`、`x = -3`
      `%`              其左紧邻是数字：`50%`

    「数字」用 `str.isdecimal()`（== Unicode Nd 类），与本模块 `_ORDERED_RE` 的 `\\d`
    口径一致（全角数字亦算）。刻意只认这几个 ASCII 标点、且括号即便紧挨数字也**不**
    纳入：用户拍板的放宽（`R：`→`R:` 半角冒号左邻是字母、`（1W1S）`→`(1W1S)` 括号、
    `、`→`,` CJK 语境）都不落进这些规则，仍走既有放宽。边界（串首/串尾）取空串，
    `"".isdecimal()`/`"".isalnum()` 均为 False，天然当作「非数字/非字母数字」。"""
    ch = body[i]
    prev = body[i - 1] if i > 0 else ""
    nxt = body[i + 1] if i + 1 < len(body) else ""
    if ch in (".", ",", "/", ":"):
        return prev.isdecimal() and nxt.isdecimal()
    if ch == "-":
        return nxt.isdecimal() and not prev.isalnum()
    if ch == "%":
        return prev.isdecimal()
    return False


def _stable_token(prefix: str, text: str) -> str:
    """前缀 + 精确匹配文本的 sha1 前 8 位十六进制——ASCII 字母数字，能在
    `_is_content_char` 的过滤里存活，从而带着「位置」一起流入逐行 signature。"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{digest}"


_CONSTRUCT_TOKEN_PREFIX = {"image": "IMG", "link": "LNK", "autolink": "URL"}


def _tokenize_blocks(text: str) -> str:
    """I1（Important 修复）：把图片 `![alt](url)`、行内链接 `[text](url)`、自动链接
    `<scheme:...>` 和围栏代码块「原地」替换成稳定的、内容派生的 ASCII-alnum token，
    而不是像旧实现那样整体剥离成独立列表（只在 content_invariant 里做逐字兜底比对）。
    旧实现把「构造挪到了 cell 的另一个位置」和「内容本身被改了」都变得对 signature 不
    可见——两者现在都会通过 token 的位置/取值反映到逐行 signature 里。

    先按行折叠围栏（每个围栏块整段 -> 单个 CODE<hash> token 行，hash 取整块
    原文，块内任一字符改动都会改 hash；用行式 `_fence_spans` 而非就近配对的
    DOTALL 正则），再替换图片/链接/自动链接（扫描器不会伸进已折叠的代码块里二次
    匹配）。F3：链接/自动链接和图片一同 token 化，故 URL 的位置/取值改动同样进签名；
    IMG token 的取值/前缀与旧版完全一致（既有图片签名用例零变化），LNK/URL 是新增前缀。"""
    lines, spans = _code_block_spans(text)
    if spans:
        span_end = {start: end for start, end in spans}
        out: list[str] = []
        i, n = 0, len(lines)
        while i < n:
            end = span_end.get(i)
            if end is not None:
                out.append(_stable_token("CODE", "\n".join(lines[i:end + 1])))
                i = end + 1
                continue
            out.append(lines[i])
            i += 1
        text = "\n".join(out)
    # F2（review）：**行内代码** span 原地 -> 稳定 ICODE token（逐行，围栏已在上面折叠成
    # CODE token，故其内反引号不会被二次匹配）。让 `` `a.b()` `` 的内容像图片/链接一样进
    # 逐行签名：一次只动行内代码内标点的改写会改 hash、改签名。从后往前替换保持下标稳定。
    inline_lines = text.split("\n")
    for idx, line in enumerate(inline_lines):
        code_spans = _inline_code_spans_in_line(line)
        if not code_spans:
            continue
        for start, end in reversed(code_spans):
            line = line[:start] + _stable_token("ICODE", line[start:end]) + line[end:]
        inline_lines[idx] = line
    text = "\n".join(inline_lines)
    # 图片/链接/自动链接 -> 稳定 token（模块唯一扫描器 `_find_link_constructs`）。
    # 从后往前替换，前面区间的 (start, end) 下标不受后续替换长度变化影响。
    for start, end, kind in reversed(_find_link_constructs(text)):
        prefix = _CONSTRUCT_TOKEN_PREFIX[kind]
        text = text[:start] + _stable_token(prefix, text[start:end]) + text[end:]
    return text


def content_signature(md: str) -> str:
    """剥掉格式与放宽的标点，返回有序「有意义字符序列」。放宽的**只有标点 `P*`**：标点
    默认不计入内容，但**符号 `S*`（数学/emoji/货币/修饰）算内容**（见 `_is_content_char`）；
    **例外**：数字语境里的标点（小数点/千分位/斜杠/一元负号/百分号/比值冒号）按相邻码点
    判为内容、不放宽（见 `_is_numeric_context_punct`，F2）。

    顶格（depth==cell_min，I4：与 `_normalize` 的判断口径完全一致，不再
    硬编码 depth==0）的 alpha 行（如 "A. 考量"）是分节标题——字母本身是
    内容，用整行原文（去强调符/标题符/引用符）而非 info.body（body 会把
    "A" 当 marker 剥掉）。缩进（depth>cell_min）的 alpha，以及所有
    bullet/ordered 的 marker 字符本身，照常剥离——但 I1：ordered marker 的
    数字是内容，不剥（rule_normalize 重排时永远原样保留数字，所以合法的
    格式重排不受影响；LLM 把 "2018." 编号成 "1." 这种数字级改动能被正确
    识别为内容变化）。
    """
    text = md or ""
    text = _tokenize_blocks(text)
    raw_lines = text.split("\n")
    infos = [classify_line(l) for l in raw_lines]
    non_blank = [info for info in infos if info.kind != "blank"]
    cell_min = min((info.depth for info in non_blank), default=0)

    lines: list[str] = []                   # 逐行独立累积，行与行之间不互通字符
    for raw_line, info in zip(raw_lines, infos):
        if info.kind == "blank":
            body = ""
        elif info.kind == "alpha" and info.depth == cell_min:
            body = raw_line                  # 顶格分节标题：字母算内容，保留整行原文
        elif info.kind == "ordered":
            body = f"{info.marker}{info.body}"   # 编号数字算内容，不再被剥掉
        else:
            body = info.body
        body = _EMPHASIS_RE.sub("", body)    # 剥强调符
        body = body.lstrip("#> ").strip()    # 剥标题符/引用符
        # F2：符号/字母/CJK/组合标记走无上下文的 _is_content_char；标点默认放宽，但
        # **数字语境**里的标点（小数点/千分位/斜杠/一元负号/百分号/比值冒号）按相邻
        # 码点判为内容，见 _is_numeric_context_punct。
        kept = "".join(
            ch
            for i, ch in enumerate(body)
            if _is_content_char(ch) or _is_numeric_context_punct(body, i)
        )
        if kept:
            lines.append(kept)               # 空行/无内容行不产生空元素，避免引入伪造的行边界
    return "\n".join(lines)


def _image_refs(md: str) -> list[str]:
    """逐字图片引用列表——走模块唯一扫描器 `_find_link_constructs` 的 image 视图
    （`_find_images`），每条为其区间内的原文，含完整的目的地 URL（哪怕 URL 里有平衡
    的内层括号）。"""
    text = md or ""
    return [text[start:end] for start, end in _find_images(text)]


def _link_refs(md: str) -> list[str]:
    """F3：逐字**行内链接 + 自动链接**引用列表——同一份 `_find_link_constructs`（filter
    link/autolink，图片仍走 `_image_refs`），每条为其区间内的原文，含完整目的地。让
    `[text](dest)`/`<scheme:...>` 的目的地和图片一样受逐字保护：一次只动 URL 标点的
    LLM 改写（`https://host/a-b`→`https://host/ab`，散文签名放宽标点看不见）会被这里
    的逐字比对拒绝。"""
    text = md or ""
    return [text[start:end] for start, end, kind in _find_link_constructs(text) if kind in ("link", "autolink")]


def _code_blocks(md: str) -> list[str]:
    """围栏代码块逐字列表——行式扫描（同一份 `_fence_spans`），每块为其起止行
    之间的原文（含围栏行）按 ``\\n`` 拼接。"""
    lines, spans = _code_block_spans(md)
    return ["\n".join(lines[start:end + 1]) for start, end in spans]


def _inline_code_refs(md: str) -> list[str]:
    """F2（review）：逐字**行内代码** span 列表（**排除**围栏代码块内的行——那些已由
    `_code_blocks` 逐字保护，别重复计数），每条为其区间内的原文（含定界反引号）。让
    `` `a.b()` `` 的内容和图片/链接/围栏一样受逐字保护：一次只动行内代码内标点的 LLM 改写
    （`` `a.b()` ``→`` `a-b[]` ``，散文签名放宽标点看不见）会被这里的逐字比对拒绝。围栏
    **先排除**（`_code_block_spans` 的行式扫描），与 `_tokenize_blocks` 的处理顺序一致。"""
    lines, spans = _code_block_spans(md)
    fenced: set[int] = set()
    for start, end in spans:
        fenced.update(range(start, end + 1))
    refs: list[str] = []
    for idx, line in enumerate(lines):
        if idx in fenced:
            continue
        for start, end in _inline_code_spans_in_line(line):
            refs.append(line[start:end])
    return refs


def content_invariant(before: str, after: str) -> bool:
    """LLM 重排前后是否只改了格式：signature 相等，且图片引用、行内链接/自动链接引用、
    围栏代码块内容、**行内代码 span 内容**（F2）逐字未变。

    `_image_refs`/`_link_refs`/`_code_blocks` 的逐字列表比对与 `content_signature`
    内部的 token 化是刻意的 belt-and-suspenders 双保险：前者按「原始匹配文本」的精确
    顺序比较（连构造彼此之间的相对顺序都会体现），后者把 token 织回逐行 signature 参与
    「跟周围文字的相对位置」比较——两套检查覆盖的角度不完全重叠，都留着。

    F3：`_link_refs` 把行内链接/自动链接的目的地纳入与图片同级的逐字保护。引用式链接
    定义 `[label]: dest` 刻意**不**纳入——它是**行**构造（本模块的构造扫描器是**字符**
    级的，不解析它），且规则门 `_starts_block_construct` 的 `_REF_LINK_DEF_RE` 对任何以
    `[..]:` 开头的行本就整格拒绝规整（rule 路径永不改写它），在 LLM 输入里又罕见——
    为它加一套行式解析与收益不成比例。"""
    if content_signature(before) != content_signature(after):
        return False
    if _image_refs(before) != _image_refs(after):
        return False
    if _link_refs(before) != _link_refs(after):
        return False
    if _code_blocks(before) != _code_blocks(after):
        return False
    if _inline_code_refs(before) != _inline_code_refs(after):
        return False
    return True
