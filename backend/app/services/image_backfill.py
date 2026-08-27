"""来源图片离线外科回填的**纯逻辑**层（无数据库、无模型、无网络）。

背景：历史部署把 PDF 用离线 MinerU 转成 markdown 后以单个 `.md` 上传入库。单文件
markdown 解析路径不传 `resolve_image`（`parsers.parse_markdown`），而空 alt 的
`![](images/<sha>.jpg)` 在 `parse_markdown_text` 的 image 分支被
`if not caption and not description and not asset_id: continue` 整块丢弃，所以这批
来源既没有图片元素、也没有资产。MinerU 的 output 树里图片文件名就是内容哈希，
仍可按文件名找回其中一部分。

本模块只做"读 markdown、对齐既有元素、算出该插入什么"，产出一份
`SourcePlan`；落库、写盘与事务边界归 `batch_ingest` 的 `backfill-images` 阶段。
拆开是为了让最容易错的那半（对齐/锚定/图注收割）可以在没有数据库的情况下逐例
测试。

**刻意登记的偏离**：本工具会把（可能无图注的）图片元素 append 进 chunk 的
`element_ids`，而标准分块管线（`chunking.build_chunks`）对无图注、无描述的
image 元素一律跳过。这是一次针对"历史数据修复"的定向偏离：判据是 markdown 里
图片引用与该段文字的物理相邻关系（原始 PDF 的版面顺序），而不是分块管线所依赖
的"这张图自带可检索文本"。答案带图的准入只有两条（`evidence_context`：
`element_type='image'` 且 `metadata.asset_id` 非空），图注**不是**显示的必要
条件，所以补进去的无图注图片照样能在引用里显示。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------- 协议边界常量
# 这些不是"可调预算"，而是对齐算法自身的形状约束（见 CLAUDE.md「数值上限与截断」：
# 不可调的协议边界复用具名常量，部署需要调整的预算才进 Settings）。图片张数与
# 单图字节这两个**真正的部署预算**一律读 Settings 现值，本模块不重新定义。

#: 单调双指针在元素序列上向前查找匹配时的前瞻窗口。MinerU 的 markdown 段落与
#: 元素基本一一对应，窗口只用来跨过解析路径丢弃掉的行（空 alt 图片、被折进上一
#: 条元素的图片描述块等）。
ALIGN_LOOKAHEAD = 8

#: 结构上**不可能**被 markdown 的"简单文本行"匹配上的元素类型。`scan_markdown`
#: 把表格行归 ``table``、围栏内归 ``code``、整行图片归 ``image``，三者都不参与
#: 匹配，所以元素侧这几类元素永远等不到属于自己的行。
#:
#: 它们必须**不消耗前瞻预算**：预算是用来容忍"解析路径丢掉了几行"的，不是用来
#: 跨过一串结构性缺席者。连续 8 张表格（或 8 张带 alt 的图片）就能把窗口吃干，
#: 此后指针**永久**停滞——而停滞是静默的：`matched` 不再增长，于是文档后半段
#: 的每一张图都锚到漂移点之前的那条元素上，被 append 进错误的 chunk，
#: `skipped` 里一个字都不会出现。实测两种形态各自把覆盖率打到 0.33。
UNMATCHABLE_ELEMENT_TYPES = frozenset({"image", "figure", "table", "code_block"})

#: 结构块（markdown 侧）-> 它在元素侧对应的类型。扫描时**即时**跨越：一个表格块
#: 推进一条 table 元素、一个围栏代码块推进一条 code_block、一条带 alt 的独立图片行
#: 推进一条 image/figure。
#:
#: 不即时跨越的后果是**静默的位置错误**：结构行不参与匹配，结构元素要等到后面某个
#: 文本行命中才被顺带并进 `matched`，于是紧跟在表格/代码块之后的独立图片会锚到结构
#: 块**之前**的那个段落、被 append 进错误的 chunk——而覆盖率仍是 100%，一个信号都
#: 没有（实测：表格后的 `![](z.jpg)` 锚到 el-0001 而不是 el-0002）。
STRUCTURAL_ELEMENT_TYPES: Mapping[str, tuple[str, ...]] = {
    "table": ("table",),
    "code_block": ("code_block",),
    "image": ("image", "figure"),
}

#: 锚点新鲜度：自锚点被匹配以来，已经有这么多条 markdown 文本行**没能**匹配上
#: 任何元素时，这个锚点就不再可信——对齐正在漂，插进去的图会落在错误的位置上
#: 且没有任何信号。与 `ALIGN_LOOKAHEAD` 同量级（两者描述的是同一件事的两个
#: 方向：能容忍多少缺席），但刻意各写各的值——改前瞻窗口不该顺手改新鲜度判据。
ANCHOR_STALE_TEXT_LINES = 8

#: 整源对齐可信度下限：低于它就整源跳过（reason ``alignment_drifted``），一张
#: 图都不插。
#:
#: 标定依据：本工具的目标语料是 MinerU 转出的 markdown，它的段落**是单行**
#: （一个 markdown 文本行 ≈ 一条元素），所以正常来源的覆盖率应当贴近 1.0；跨过
#: 不可匹配元素之后，两个已知漂移形态（连续表格、连续带 alt 图片）实测都回到
#: 1.00。0.2 的余量留给"解析路径把相邻几行折进了同一条元素"这类零星差异。低于
#: 这条线时双指针已经不知道自己走到哪了，每个锚点都是猜的——宁可整源不补，也
#: 不能把图插进错误的 chunk。
#:
#: 已登记的代价：手写、硬折行的 markdown（一个自然段跨多行）会被这道闸整源挡
#: 掉。方向是安全的（跳过而不是错插），且在 stdout 汇总与 `--report` 里以
#: ``alignment_drifted`` 逐源可见，不是静默失败。
MIN_ALIGNMENT_COVERAGE = 0.8

#: 锚点元素不在任何 chunk 时，沿对齐序向前回退的最大步数。
CHUNK_LOOKBACK = 12

#: 图注收割时，向图片行前后各看几行。
CAPTION_SCAN_LINES = 3

#: 前缀匹配至少要有这么多字符才算数，避免"第一章"匹配上任意以它开头的段落。
MIN_PREFIX_MATCH_CHARS = 12

#: 同一锚点下的图片序号位宽。**三位**不是两位：每源上限是部署设置
#: （`MINERU_MAX_IMAGES_PER_SOURCE`，默认 200），一个锚点底下挂过 99 张之后，
#: 两位的 `-g99` 与三位才写得下的第 100 张在 C collation 下就不再单调
#: （`"-g100" < "-g99"`），元素顺序会当场乱掉。本特性未发布，没有兼容包袱。
#:
#: 位宽是**固定**的，而每源上限是**部署可配的且没有上界校验**——所以两者必须
#: 在这一侧闭合：序号一旦要越过 `MAX_ANCHOR_SUFFIX`（999）就跳过那张图并记
#: `anchor_suffix_exhausted`，而不是铸出一个 `-g1000` 把排序毁掉。刻意不去给
#: `MINERU_MAX_IMAGES_PER_SOURCE` 加上界校验：那是**在线解析路径也在用**的共享
#: 设置，为一个离线修复工具的 id 形状去收紧它会改变别人的行为面；有界跳过只影响
#: 本工具自己，且在 report 里看得见。
ANCHOR_SUFFIX_WIDTH = 3

#: 缩进到这么多列（tab 按 4 列）就不再是普通正文行。markdown 的缩进代码块判据是
#: 4 列；列表续行与段落续行同样缩进，而这三种形态实测都**不**产出 image 块，所以
#: 对"这一行是不是真图"这个问题它们同答一个"不是"，一条判据就够。
INDENTED_CODE_COLUMNS = 4

#: 同一锚点下能容纳的最大图片序号（由位宽派生，别写第二个字面量）。
MAX_ANCHOR_SUFFIX = 10 ** ANCHOR_SUFFIX_WIDTH - 1


# ------------------------------------------------------------------ markdown 扫描

#: `![alt](target)`、`![alt](<target>)`、`![alt](target "title")` 三种形态。
#: 不认引用式 `![alt][ref]`：MinerU 不产出它，而支持它需要另解析链接定义表。
_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\("
    r"\s*(?P<target><[^<>]*>|[^()\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*"
    r"\)"
)

#: 远程 / data URI：两者都不代下、不处理（`data:` 由在线解析路径的
#: `_persist_markdown_data_uri` 负责，回填不重复那条路）。
_REMOTE_OR_DATA = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")

#: 围栏代码块（``` 或 ~~~，允许缩进与 info string）。
_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")

#: 图注判据：`Figure 1` / `Fig. 2` / `表 3` / `图 4-1` 这类"名词 + 数字"。刻意
#: 收窄成必须带数字：没有编号的普通句子是正文而不是图注，收它进 caption 会把
#: 相邻段落的第一句话冒充成图片说明。
_CAPTION_RE = re.compile(
    r"^(?:图表|图|表|Figure|Fig|Table|Tab|Chart|Scheme|Exhibit)"
    r"\s*[.:：、\-–—]?\s*\d",
    re.IGNORECASE,
)

#: 行首的列表标记 / 引用标记 / 标题井号。
_LINE_DECORATION_RE = re.compile(r"^\s*(?:[>#]+\s*|[-*+]\s+|\d+[.)]\s+)+")

#: 归一化时剥掉的行内 markdown 装饰字符。
_INLINE_MARKS_RE = re.compile(r"[*_`~]+")


@dataclass(frozen=True)
class ImageRef:
    """markdown 里的一处图片引用。"""

    line: int
    """0-based 行号。"""

    ordinal: int
    """在**全部**图片引用（含远程/data）里的 1-based 序号。稳定跨重跑，用于
    `location_label`，与 `parse_markdown_text` 的 per-type 计数器同款语义。"""

    alt: str
    src: str
    """原样的引用目标（已剥 `<>` 与 query/fragment），相对路径。"""

    kind: str
    """``"relative"`` / ``"remote"`` / ``"data"``。"""

    image_block: bool
    """这处引用所在的行确实是一个**真正的 image 块**。

    在线 markdown 路径只为这种行产出 image 元素并落资产。「行独占」只是必要条件，
    不是充分条件——`parse_markdown_text` 实测这几种"看起来独占一行"的形态一条
    image 元素都不产出：

    * **HTML 表块内部**的图片行（整块折成一条 table 元素）；
    * **缩进 4 列以上**的图片行（缩进代码块 / 列表续行 / 段落续行，三种实测都没有
      image 元素，tab 缩进同理）；
    * 一行里**两张以上**图片（markdown-it 把它当普通段落）。

    把它们当真图还原，就会造出一批在线路径永远不会产出的元素与资产，违背"表格/
    代码内的图片只留 alt 文本、不落资产"的产品规则。围栏代码块内的行更早就被归成
    ``code`` 并整行跳过，连引用都不会被收集。"""


@dataclass(frozen=True)
class MarkdownLine:
    index: int
    raw: str
    kind: str
    """``"text"`` / ``"image"`` / ``"table"`` / ``"code"`` / ``"blank"``。"""

    norm: str
    """归一化后的可比较文本（仅 ``kind == "text"`` 有意义）。"""

    block_start: str = ""
    """这一行**开启**了一个结构块时，它对应的元素类型；否则空串。

    取值 ``"table"`` / ``"code_block"`` / ``"image"``。只标在块的**第一行**上：
    一整张表、一个围栏代码块各自在元素侧只是**一条**元素，所以只该推进一格。"""


def normalize_text(value: str) -> str:
    """把 markdown 行与元素文本折成同一种可比较形状。

    两侧必须逐字共用这一个函数：分别写两份归一化是这类对齐算法最典型的静默
    失败——它不报错，只是永远匹配不上，于是每张图都"锚定失败"被跳过。

    图片语法折成它自己的 **alt 文本**（空 alt 折成空白）。在线解析路径对**内嵌**
    图片（段落里、列表项里、表格格子里）执行的正是"只留 alt 文本、不落资产"这条
    产品规则——`parse_markdown_text` 实测把 ``这一段里内嵌了 ![备注](x.jpg) 一张图。``
    产出成元素正文 ``这一段里内嵌了 备注 一张图。``。把整段图片语法连 alt 一起抹掉
    的话，这一行永远匹配不上它自己的元素：短文档里一行就足以把覆盖率压到
    `MIN_ALIGNMENT_COVERAGE` 之下、让整源被 `alignment_drifted` 误跳，长文档里则是
    白丢一个锚点。"""
    text = _LINE_DECORATION_RE.sub("", value or "")
    # 两侧留空格：alt 不能与相邻文字粘连（元素侧那一半也是靠空白分开的），随后
    # 的 `split()` 会把多余空白收掉。
    text = _IMAGE_RE.sub(lambda match: f" {match.group('alt') or ''} ", text)
    text = _INLINE_MARKS_RE.sub("", text)
    return " ".join(text.split())


def _norm_without_image_alt(value: str) -> str:
    """行**分类**专用：把图片语法连 alt 一起抹掉后还剩什么。

    刻意与 `normalize_text` 分开，两者回答的是不同的问题：这里问"这一行除了图片
    之外还有没有别的内容"（决定它是不是可匹配的正文行），`normalize_text` 问"这
    一行折成元素正文长什么样"（决定它匹不匹配得上）。

    合并成一个会当场回归：独占一行的 `![图 1 …](p1.jpg)` 在解析路径上产出的是一条
    **image 元素**（`UNMATCHABLE_ELEMENT_TYPES`，指针无代价跨过、从不参与匹配）。
    若按带 alt 的归一化去分类，这行就成了"正文行"，于是它进 `text_lines` 分母却
    永远匹配不上——9 张连续带图注的图片会把覆盖率从 1.00 打到 0.25，正好把
    `test_a_run_of_captioned_images_…` 要守的那条闸反向踩塌。"""
    text = _LINE_DECORATION_RE.sub("", value or "")
    text = _IMAGE_RE.sub(" ", text)
    text = _INLINE_MARKS_RE.sub("", text)
    return " ".join(text.split())


def _leading_columns(raw: str) -> int:
    """行首缩进了多少列（tab 按 4 列，与 markdown 的缩进代码块口径一致）。"""
    width = 0
    for char in raw:
        if char == " ":
            width += 1
        elif char == "\t":
            width += INDENTED_CODE_COLUMNS
        else:
            break
    return width


def canonical_src(value: str) -> str:
    """一处图片引用的**规范形**：剥掉 query/fragment（它们是 URL 语法，不属于存下
    来的文件名；与 `_bundle_image_path` 同款处理）。

    这一个函数必须同时用在**两侧**：扫描 markdown 得到的引用，以及既有元素
    ``metadata.src`` 拿来做集合键的时候。解析路径把 ``![Figure 1](images/a.jpg?raw=1#x)``
    的 target **原样全量**存进 ``metadata.src``，而扫描侧剥掉了后缀——只在一侧规范化
    的后果是"已补过/可补齐"两个集合永远匹配不上这类引用：既有元素补不上 asset，还会
    被当成全新的图再插一条重复元素。新插入元素写进 ``metadata.src`` 的同样是这个规范
    形（`_element_row`），所以本工具自己产出的行天然自洽。"""
    target = (value or "").strip()
    for sep in ("#", "?"):
        cut = target.find(sep)
        if cut >= 0:
            target = target[:cut]
    return target.strip()


def _strip_target(raw: str) -> str:
    target = (raw or "").strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return canonical_src(target)


def classify_target(target: str) -> str:
    if not target:
        return "remote"
    lowered = target[:5].lower()
    if lowered.startswith("data:"):
        return "data"
    if _REMOTE_OR_DATA.match(target):
        return "remote"
    return "relative"


def scan_markdown(text: str) -> tuple[list[MarkdownLine], list[ImageRef]]:
    """把 markdown 文本切成带类型的行 + 全部图片引用。

    行分类只服务于对齐：围栏代码块内部一律 ``"code"``（那里的文字不是正文，
    元素侧是一整条 code_block 元素），表格行 ``"table"``（`parse_blocks` 把整张
    表折成一条元素，逐行去匹配只会把指针推过头）——**HTML 表格块也算**：MinerU 与
    PyMuPDF4LLM 会产出 ``<table>…</table>``，`parse_blocks` 同样把整块折成一条
    table 元素（实测），而它不以 ``|`` 开头，不专门认就会被当成一串永远匹配不上的
    正文行。

    每个结构块的**第一行**还带 `MarkdownLine.block_start`，供对齐即时跨越。"""
    lines: list[MarkdownLine] = []
    refs: list[ImageRef] = []
    fence: str | None = None
    in_html_block = False
    prev_pipe = False
    ordinal = 0
    for index, raw in enumerate((text or "").splitlines()):
        fence_match = _FENCE_RE.match(raw)
        if fence is not None:
            # 围栏内：只认闭合围栏，其余一律 code。
            lines.append(MarkdownLine(index, raw, "code", ""))
            if fence_match and fence_match.group("fence")[0] == fence[0] and len(
                fence_match.group("fence")
            ) >= len(fence):
                fence = None
            prev_pipe = False
            continue
        if fence_match:
            # **开栏行无条件是新结构块的起点**——判据是显式的开/闭状态，不是"上一行
            # 是不是 code"。两个围栏块相邻无空行时，第一个闭栏之后"上一行仍是 code"
            # 依然成立，于是第二个块拿不到 `block_start`，对齐只跨一个 code_block
            # 元素，紧随第二个块之后的图片静默锚到**第一个**块上（coverage 仍是
            # 100%，一个信号都没有）。
            fence = fence_match.group("fence")
            lines.append(MarkdownLine(index, raw, "code", "", "code_block"))
            prev_pipe = False
            continue

        # HTML 块的状态机（围栏之后判，好让围栏里的 `<table>` 保持 code）。终止判据
        # 是**空行**而不是 `</table>`：markdown-it 的 HTML 块规则一直吃到空行为止，
        # 实测紧跟在 `</table>` 后面（无空行）的正文会被折进同一条 table 元素——按
        # `</table>` 收尾就会把那一行当成永远匹配不上的正文行，白掉一格覆盖率。
        html_open = False
        if in_html_block:
            if not raw.strip():
                in_html_block = False  # 空行终止 HTML 块；这一行自己是 blank
        elif raw.strip().lower().startswith("<table"):
            html_open = True
            in_html_block = True

        # 「这一行是不是一个真正的 image 块」——准入与序号共用的**唯一**判据
        # （见 `ImageRef.image_block`）。「剥掉图片后还剩什么」按**原始**行判：刻意
        # 不复用 `normalize_text`，它会先剥掉行首的列表/引用/标题标记，于是
        # `- ![](b.jpg)` 会被判成"只有图片"，而在线路径对它产出的是 list_item。
        matches = list(_IMAGE_RE.finditer(raw))
        image_block = (
            len(matches) == 1
            and not (html_open or in_html_block)
            and _leading_columns(raw) < INDENTED_CODE_COLUMNS
            and not _IMAGE_RE.sub("", raw).strip()
        )
        line_refs: list[ImageRef] = []
        for match in matches:
            # 序号只数真正的 image 块，与 `parse_markdown_text` 的 per-type 计数器
            # 同口径：行内引用、结构块内引用、多图行都不占号，否则还原出来的独立图
            # 会标成 `Markdown image 2` 而规范解析器标 `1`。
            #
            # 已登记的残余：规范解析器把**空 alt** 的图片在 block 层就丢掉（实测
            # `parse_blocks` 对 `![](a.jpg)` 根本不产 image 块），而那正是本工具要
            # 还原的那一批——它们没有可对齐的规范号，所以这里按文档序照常给号。
            # 全部独立图都带 alt 的文档里两侧逐字相同；混合文档只保证号在源内唯一
            # 且跨重跑稳定。
            if image_block:
                ordinal += 1
            target = _strip_target(match.group("target"))
            line_refs.append(
                ImageRef(
                    line=index,
                    ordinal=ordinal if image_block else 0,
                    alt=(match.group("alt") or "").strip(),
                    src=target,
                    kind=classify_target(target),
                    image_block=image_block,
                )
            )
        refs.extend(line_refs)

        stripped = raw.strip()
        pipe = False
        if html_open or in_html_block:
            # HTML 块整块归 table：元素侧它只是一条 table 元素。
            kind = "table"
        elif not stripped:
            kind = "blank"
        elif line_refs and not _norm_without_image_alt(raw):
            # 整行只有图片（把图片语法连 alt 一起抹掉后什么都不剩）：它是锚定的
            # 定位点，不是可匹配的正文行。分类**必须**用抹掉 alt 的那份归一化，
            # 理由见 `_norm_without_image_alt` 的 docstring。
            kind = "image"
        elif stripped.startswith("|"):
            kind = "table"
            pipe = True
        else:
            kind = "text"

        block_start = ""
        if html_open:
            # 一个 HTML 块的开头。**不能**按"上一行是不是 table"判：`| a |` 管道表格
            # 紧跟一个 `<table>` 时两者是**两条**元素（实测），而上一行正是 table。
            block_start = "table"
        elif pipe and not prev_pipe:
            # 连续管道行折成一条 table 元素，所以只在这一串的**第一行**起块。反过来
            # 说，`<table>` 之后紧跟的管道行属于那个 HTML 块（上面 kind 已归 table、
            # pipe 保持 False），不会重复起块。
            block_start = "table"
        elif image_block and any(ref.alt for ref in line_refs):
            # 只对**带 alt** 的独立图片行跨越：空 alt、无描述、无资产的图片被
            # `parse_markdown_text` 整块丢弃（`if not caption and not description
            # and not asset_id: continue`），元素侧根本没有它，跨过去就会吃掉后面
            # 某张图的元素。
            block_start = "image"
        lines.append(
            MarkdownLine(
                index,
                raw,
                kind,
                normalize_text(raw) if kind == "text" else "",
                block_start,
            )
        )
        prev_pipe = pipe
    return lines, refs


# ------------------------------------------------------------------ 图片索引

@dataclass(frozen=True)
class ImageIndexEntry:
    path: Path
    size: int


@dataclass
class ImageIndex:
    """MinerU output 树的 ``文件名 -> (路径, 字节数)`` 索引。

    只认父目录名为 ``images`` 的文件——`auto`/`ocr`/`txt` 三种方法目录都在它上面
    一层，认父目录名比认完整路径形状更宽容，也避免把 output 树里别的 png（例如
    版面可视化）当成正文插图。"""

    entries: dict[str, ImageIndexEntry] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)
    """同名但字节数不同的文件名（按先见者取，此处只记警告）。"""

    def get(self, name: str) -> ImageIndexEntry | None:
        return self.entries.get(name)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.entries)


def build_image_index(roots: Sequence[Path]) -> ImageIndex:
    """整树扫描建索引。

    遍历用 `os.walk` 并就地排序 ``dirnames``/``filenames``，而**不是**
    ``sorted(root.rglob("*"))``：后者要先把整棵树的每一个条目（真实部署上是约
    4.9 万份文档的 output 树，条目数以百万计）物化成一个 `Path` 列表再排序，
    峰值内存与树的大小成正比。逐目录排序给出的是同一个确定性顺序（父目录字典
    序、目录内文件字典序），"同名先见者取"的语义因此逐字不变。"""
    index = ImageIndex()
    for root in roots:
        if not root.is_dir():
            continue
        for parent, dirnames, filenames in os.walk(root):
            dirnames.sort()  # 就地排序 = 决定 os.walk 后续的下降顺序
            if Path(parent).name != "images":
                continue
            for name in sorted(filenames):
                path = Path(parent) / name
                try:
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                except OSError:
                    continue
                existing = index.entries.get(name)
                if existing is not None:
                    if existing.size != size and name not in index.duplicates:
                        index.duplicates.append(name)
                    continue
                index.entries[name] = ImageIndexEntry(path=path, size=size)
    return index


# ------------------------------------------------------------------ 对齐 / 锚定

@dataclass(frozen=True)
class ElementView:
    """既有元素在对齐与预算里用到的那一小部分。"""

    id: str
    element_type: str
    norm: str

    has_asset: bool = False
    """这条 image 元素已经挂着一个资产（``metadata.asset_id`` 非空）。

    每源图片上限数的是**已持久化的图片**（在线路径的 `persist_image` 闭包按落资产
    次数计数），所以只有它才是预算的分母；带图注却没挂资产的历史元素既显示不出来
    也没占用配额，要等被补齐时才计入。"""


def _matches(line_norm: str, element_norm: str) -> bool:
    if not line_norm or not element_norm:
        return False
    if line_norm == element_norm:
        return True
    # 元素文本可能横跨多行（`parse_blocks` 把连续行折成一段），也可能被
    # `_element` 压平后比单行更长；两个方向都允许有界前缀匹配。
    shorter, longer = (
        (line_norm, element_norm)
        if len(line_norm) <= len(element_norm)
        else (element_norm, line_norm)
    )
    return len(shorter) >= MIN_PREFIX_MATCH_CHARS and longer.startswith(shorter)


@dataclass
class Alignment:
    """一次对齐的结果：每一行看到的锚点，以及匹配序列本身。"""

    position_by_line: dict[int, int]
    """行号 -> 该行处理完之后的锚点在 ``matched`` 里的下标；``-1`` 表示还没有
    任何已匹配元素（合成基座）。

    刻意对**每一行**都记而不只是整行图片：行内内嵌图片（``见 ![](a.jpg) 所示``）
    所在的行自己是一条正文行，它若匹配上了元素，锚点就该是它自己而不是上一
    条。记账时机统一在"处理完这一行之后"，两种形态因此共用同一条规则，不需要
    第二份判据（第二份判据正是这类对齐算法最容易静默走偏的地方）。"""

    stale_by_line: dict[int, int]
    """行号 -> 该行之前、自上一次成功匹配以来连续未匹配的**文本行**条数。

    锚点新鲜度判据：这个数一旦超过 `ANCHOR_STALE_TEXT_LINES`，
    ``position_by_line`` 指向的那个锚点就只是"最后一次还认得路的地方"，而不是
    这张图物理上跟着的那条元素。"""

    matched: list[str]
    """指针**确实走过**的元素 id，按对齐顺序。

    既包含真正被文本行匹配上的元素，也包含在一次成功匹配途中被跨过的不可匹配
    元素（表格/代码块/图片）——它们同样在指针身后，锚点回退时理应看得见它们，
    尤其带图注的历史图片元素是进过 chunk 的、是比更早那条段落更近的落点。"""

    matched_lines: int
    text_lines: int

    @property
    def coverage(self) -> float:
        return (self.matched_lines / self.text_lines) if self.text_lines else 0.0


def align(lines: Sequence[MarkdownLine], elements: Sequence[ElementView]) -> Alignment:
    """markdown 行与既有元素的单调双指针对齐。

    单调是关键：元素 id 字典序就是文档序（`el-<sid>-NNNN` 零补位，`chunking`
    的读取按 ``ORDER BY id``），所以指针只前进不回退，一旦对齐漂掉就自然停止
    匹配，而不是在文档后半段乱认。

    前瞻预算只对**可匹配**元素计数：`UNMATCHABLE_ELEMENT_TYPES` 里的元素在
    markdown 侧没有任何"简单文本行"与之对应，指针无代价跨过它们（否则一串连续
    表格/带 alt 图片就把窗口吃干，指针永久停滞，见那个常量的注释）。跨过的元素
    只有在这一轮**真的**匹配成功时才计入 `matched`——没找到落脚点就不算走过，
    宁可保守。"""
    position_by_line: dict[int, int] = {}
    stale_by_line: dict[int, int] = {}
    matched: list[str] = []
    cursor = 0
    matched_lines = 0
    text_lines = 0
    stale = 0

    def cross_structural(block: str) -> None:
        """把指针**即时**推过这个结构块对应的元素。

        只在游标处（跨过其间的不可匹配元素之后）**恰好**是期望类型时才推进：期望
        类型不在眼前就什么都不做。宁可不推进（退回旧的滞后行为）也不猜——推错一格
        比晚一格坏得多。"""
        nonlocal cursor
        wanted = STRUCTURAL_ELEMENT_TYPES.get(block, ())
        index = cursor
        while index < len(elements) and (
            elements[index].element_type in UNMATCHABLE_ELEMENT_TYPES
        ):
            if elements[index].element_type in wanted:
                matched.extend(
                    elements[position].id for position in range(cursor, index + 1)
                )
                cursor = index + 1
                return
            index += 1

    for line in lines:
        if line.block_start:
            # 必须在记 `position_by_line` **之前**跨越：紧跟在结构块之后的那张独立
            # 图片要锚到结构元素本身（表格/代码块都在 chunk 里，chunk 归属因此自然
            # 正确），而不是结构块**之前**的那个段落。
            cross_structural(line.block_start)
        if line.kind == "text":
            text_lines += 1
            budget = ALIGN_LOOKAHEAD
            crossed: list[str] = []
            index = cursor
            hit = False
            while index < len(elements) and budget > 0:
                element = elements[index]
                if element.element_type in UNMATCHABLE_ELEMENT_TYPES:
                    crossed.append(element.id)
                    index += 1
                    continue
                if _matches(line.norm, element.norm):
                    matched.extend(crossed)
                    matched.append(element.id)
                    cursor = index + 1
                    matched_lines += 1
                    stale = 0
                    hit = True
                    break
                budget -= 1
                index += 1
            if not hit:
                stale += 1
        position_by_line[line.index] = len(matched) - 1
        stale_by_line[line.index] = stale
    return Alignment(
        position_by_line=position_by_line,
        stale_by_line=stale_by_line,
        matched=matched,
        matched_lines=matched_lines,
        text_lines=text_lines,
    )


def harvest_caption(lines: Sequence[MarkdownLine], image_line: int) -> str:
    """图片行前后最近的非空行里机会性地取一条图注。

    先看下方再看上方（MinerU 的版面还原把图注排在图下是常态），不跨越另一张
    图，最多各看 ``CAPTION_SCAN_LINES`` 行。收不到就空着——显示不依赖图注。

    **刻意不截断**：原规格要求"与解析路径的既有 caption 上限对齐"，而查过
    `parsers.parse_markdown_text` 的 image 分支后确认那条路径**没有**任何 caption
    长度常量——`metadata["caption"]` 与元素文本都是整段 alt/图注原样落库。这里
    自造一个上限就等于让回填出来的行比在线解析出来的行更短，两批数据在同一张
    表里对不齐；对齐的做法是同样不截断。图注长度的真实上界由 markdown 的一行
    与上面那个"最近的非空行"判据兜住。"""
    by_index = {line.index: line for line in lines}
    for step in (1, -1):
        for offset in range(1, CAPTION_SCAN_LINES + 1):
            line = by_index.get(image_line + step * offset)
            if line is None:
                break
            if line.kind == "image":
                break  # 不跨图
            if line.kind == "blank":
                continue
            if line.kind != "text":
                break
            candidate = line.norm
            if candidate and _CAPTION_RE.match(candidate):
                return candidate
            break  # 最近的非空行不是图注就放弃这个方向
    return ""


# ------------------------------------------------------------------ 计划

@dataclass(frozen=True)
class PlannedImage:
    element_id: str
    anchor_element_id: str
    chunk_id: str
    src: str
    caption: str
    ordinal: int
    source_path: Path
    size: int
    line: int


@dataclass(frozen=True)
class EnrichedImage:
    """一条**既有**图片元素，位置与图注都已在库里，只差 ``metadata.asset_id``。

    生产解析路径对带 alt 的相对路径图片（``![图 1 架构](images/a.jpg)``）会产出
    一条 image 元素、写下 ``metadata.src``，但因为单文件 markdown 路径不传
    `resolve_image`，它拿不到 ``asset_id``（`parsers.parse_markdown_text` 的
    image 分支：``else: metadata["src"] = src``）。这类元素既不在"已补过"的集合
    里（判据是 asset_id 非空），又与本次引用指向同一个 src——只按"插入"处理就会
    给同一张图造出第二条元素行。

    对原规格"只插入、不修改既有元素行"的一次**显式修订**：改为**就地补齐**
    ``metadata.asset_id``（text/caption/id/created_at 一律不动）。理由是这条元素
    已经在正确位置上、带图注者也已经在 chunk 里，补出第二条只会制造重复行，而
    重复行既占检索位又会让引用带图重复显示同一张图。"""

    element_id: str
    src: str
    source_path: Path
    size: int
    chunk_id: str
    """需要把该元素 id append 进去的 chunk；空串表示它已经在某个 chunk 里，
    本次 chunk 零改动。"""

    has_caption: bool = False
    """这条既有元素自带可检索文本（图注/图片描述）。

    补齐不改写 ``text``，所以图注统计的权威是元素自己，见 `_element_has_text`。"""


@dataclass
class SourcePlan:
    source_id: str
    images: list[PlannedImage] = field(default_factory=list)
    enriched: list[EnrichedImage] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    """稳定 reason code -> 计数。绝不含图片字节或正文。"""

    captions: int = 0
    coverage: float = 0.0

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + count


def plan_source_images(
    *,
    source_id: str,
    markdown: str,
    elements: Sequence[ElementView],
    existing_image_srcs: Iterable[str],
    existing_unassigned_srcs: Mapping[str, str],
    existing_element_ids: Iterable[str],
    chunk_by_element: Mapping[str, str],
    image_index: ImageIndex,
    max_images: int,
    max_bytes: int,
) -> SourcePlan:
    """算出这个来源应该补哪些图片元素、每张挂在哪个 chunk 上。

    纯函数：不读数据库、不写盘。

    ``existing_image_srcs`` 是本来源已有的、带 ``metadata.asset_id`` 的图片元素
    的 ``metadata.src`` 集合——它就是重跑的增量判据（找回更多原图之后再跑一次只
    会补新的，已补的一张都不会重写）。

    ``existing_unassigned_srcs`` 是 ``src -> element_id``，覆盖另一半：已有 image
    元素、``metadata.src`` 对得上、但 ``asset_id`` **为空**的那些行（见
    `EnrichedImage`）。同一个 src 底下有多条既有元素时只认 id 序第一条、其余一
    律不动：多条同 src 的元素是历史畸形数据，补齐其中一条已经让这张图显示得
    出来，把每一条都改成同一个资产只会让引用带图重复显示同一张图。
    """
    plan = SourcePlan(source_id=source_id)
    lines, refs = scan_markdown(markdown)
    alignment = align(lines, elements)
    plan.coverage = alignment.coverage
    drifted = alignment.coverage < MIN_ALIGNMENT_COVERAGE

    already = {src for src in existing_image_srcs if src}
    enriched_ids: set[str] = set()
    # 同一锚点下的续号：先扫既有 id，重跑才不会撞主键。
    used_suffix: dict[str, int] = {}
    for element_id in existing_element_ids:
        head, sep, tail = element_id.rpartition("-g")
        if sep and tail.isdigit():
            used_suffix[head] = max(used_suffix.get(head, 0), int(tail))

    # 元素 id -> 它在文档序里的下标。就地补齐用它沿元素自身顺序回退找 chunk 落点，
    # 完全不碰 markdown 对齐（`elements` 已按 id 序读回，那就是文档序）。
    element_positions = {element.id: index for index, element in enumerate(elements)}

    # 每源图片预算：**插入与补齐共用**这一份。分母是已经挂着资产的既有 image 元素
    # （= 已持久化的图片），因为上限约束的正是"这个来源存了多少张图"。补齐不吃预算
    # 的话，一个既有未挂资产元素多于上限的来源会被整批放行，直接越过
    # `MINERU_MAX_IMAGES_PER_SOURCE`。
    remaining = max_images - sum(
        1
        for element in elements
        if element.element_type == "image" and element.has_asset
    )
    for ref in refs:
        if ref.kind != "relative":
            plan.skip(f"{ref.kind}_uri")
            continue
        if not ref.image_block:
            # 产品规则：只有真正的 image 块才落资产。列表项/表格单元格/段落中间的
            # 内嵌图片只留 alt 文本；HTML 表块内部、缩进 4 列以上、以及一行多图的
            # 引用同样不产 image 元素（见 `ImageRef.image_block`）。MinerU 的主流
            # 形态是图片独占一整行、无缩进，不受影响。
            plan.skip("inline_image_skipped")
            continue
        if ref.src in already:
            plan.skip("already_backfilled")
            continue
        entry = image_index.get(Path(ref.src).name)
        if entry is None:
            plan.skip("image_not_found")
            continue
        if entry.size > max_bytes:
            plan.skip("image_too_large")
            continue

        if remaining <= 0:
            # 插入与补齐共用同一份预算，所以这道闸排在两条路**之前**，reason code
            # 也共用一个：无论哪种方式，结论都是"这个来源的图片配额满了"。
            plan.skip("per_source_cap")
            continue

        existing_id = existing_unassigned_srcs.get(ref.src)
        if existing_id is not None:
            if existing_id in enriched_ids:
                # 同一个 src 在文档里被引用了多次，但只有一条既有元素可补。补第
                # 二次会让它被 append 进 chunk 两遍（`element_ids` 里出现重复
                # id），而"再插一条新元素"等于凭空造出一条在线路径不会有的行。
                plan.skip("duplicate_src_reference")
                continue
            # 就地补齐：不新增元素，因此不吃 `remaining`（这条元素本来就已经计
            # 在既有 image 元素数里了）。已经在某个 chunk 里就零改动 chunk；
            # 不在（无图注的历史元素不进 chunk）才需要找一个落点。
            #
            # 这条路**整条与 markdown 对齐无关**：目标元素是按精确 src 相等找到
            # 的，它的位置早已在库里，chunk 落点沿**元素 id 序**向前回退即可。所以
            # 漂移闸（`MIN_ALIGNMENT_COVERAGE`）刻意不管它——纯图片文档的 coverage
            # 恒为 0（一条文本行都没有），按闸拒掉就等于对这类来源永远补不了图，而
            # 这恰恰是本工具最该修的那一批。
            if chunk_by_element.get(existing_id):
                chunk_id = ""
            else:
                chunk_id = _chunk_for_preceding_element(
                    elements, element_positions, existing_id, chunk_by_element
                )
                if not chunk_id:
                    plan.skip("no_chunk")
                    continue
            enriched_ids.add(existing_id)
            plan.enriched.append(
                EnrichedImage(
                    element_id=existing_id,
                    src=ref.src,
                    source_path=entry.path,
                    size=entry.size,
                    chunk_id=chunk_id,
                    has_caption=_element_has_text(
                        elements, element_positions, existing_id
                    ),
                )
            )
            if plan.enriched[-1].has_caption:
                plan.captions += 1
            remaining -= 1
            continue

        if drifted:
            # 整源闸**只作用于新插入**：插入要靠 markdown 对齐算锚点，而对齐一旦
            # 不可信，每个锚点都是猜的（见 `MIN_ALIGNMENT_COVERAGE`）。上面的就地
            # 补齐不经过这道闸。逐张记账而不是整源记一条，是为了让 stdout 汇总里
            # 的 `alignment_drifted` 与其它 reason code 同口径（都是"多少张图没补
            # 上"）。
            plan.skip("alignment_drifted")
            continue
        anchor = _anchor_for(plan, alignment, ref, chunk_by_element)
        if anchor is None:
            continue
        anchor_id, chunk_id = anchor

        next_suffix = used_suffix.get(anchor_id, 0) + 1
        if next_suffix > MAX_ANCHOR_SUFFIX:
            # 固定位宽与可配上限的闭合点（见 `ANCHOR_SUFFIX_WIDTH`）：越过它就得
            # 铸 `-g1000`，而 C collation 下 `"-g1000" < "-g999"`，这一锚点底下
            # 的元素顺序会当场乱掉。宁可少补一张，也不能把顺序写坏。
            plan.skip("anchor_suffix_exhausted")
            continue
        used_suffix[anchor_id] = next_suffix
        caption = harvest_caption(lines, ref.line) or ref.alt
        if caption:
            plan.captions += 1
        plan.images.append(
            PlannedImage(
                element_id=f"{anchor_id}-g{next_suffix:0{ANCHOR_SUFFIX_WIDTH}d}",
                anchor_element_id=anchor_id,
                chunk_id=chunk_id,
                src=ref.src,
                caption=caption,
                ordinal=ref.ordinal,
                source_path=entry.path,
                size=entry.size,
                line=ref.line,
            )
        )
        remaining -= 1
    return plan


def _anchor_for(
    plan: SourcePlan,
    alignment: Alignment,
    ref: ImageRef,
    chunk_by_element: Mapping[str, str],
) -> tuple[str, str] | None:
    """一张图的 ``(锚点元素 id, chunk id)``；三种失败各自记账后返回 ``None``。

    三个 reason code 是三种处置完全不同的运维结论，绝不合并：

    * ``anchor_stale`` —— 锚点还在，但自它被匹配以来已经有一串文本行对不上了，
      对齐正在漂（见 `ANCHOR_STALE_TEXT_LINES`）。插进去的位置是错的。
    * ``no_anchor`` —— 这张图之前一条元素都没匹配上（典型：文档开头就是图）。
      没有锚点可用，也没有 chunk 可挂。
    * ``no_chunk`` —— 锚点找到了，但它和它之前有界范围内的元素都不属于任何
      chunk。这段文字本来就没进检索，补进去的图也不会被引用到。
    """
    if alignment.stale_by_line.get(ref.line, 0) > ANCHOR_STALE_TEXT_LINES:
        plan.skip("anchor_stale")
        return None
    position = alignment.position_by_line.get(ref.line, -1)
    if position < 0:
        plan.skip("no_anchor")
        return None
    chunk_id = _chunk_for_anchor(alignment, position, chunk_by_element)
    if not chunk_id:
        plan.skip("no_chunk")
        return None
    return alignment.matched[position], chunk_id


def _chunk_for_preceding_element(
    elements: Sequence[ElementView],
    element_positions: Mapping[str, int],
    element_id: str,
    chunk_by_element: Mapping[str, str],
) -> str:
    """就地补齐的 chunk 落点：从该元素自己出发，沿**元素 id 序**（= 文档序）有界
    向前回退，取第一个属于某个 chunk 的元素所在的 chunk；找不到返回空串。

    与新插入用的 `_chunk_for_anchor` 是同一条"向前回退"语义，区别只在走的是哪条
    序列：插入的位置只能由 markdown 对齐推出来，而补齐的目标元素**已经在库里**、
    位置是已知事实，不需要也不该依赖对齐。回退步数复用 `CHUNK_LOOKBACK`。"""
    index = element_positions.get(element_id)
    if index is None:
        return ""
    steps = 0
    while index >= 0 and steps <= CHUNK_LOOKBACK:
        chunk_id = chunk_by_element.get(elements[index].id)
        if chunk_id:
            return chunk_id
        index -= 1
        steps += 1
    return ""


def _element_has_text(
    elements: Sequence[ElementView],
    element_positions: Mapping[str, int],
    element_id: str,
) -> bool:
    """这条既有元素自带可检索文本（图注或图片描述）吗。

    补齐不改写 ``text``，所以"这张图有没有图注"的权威是**元素自己**，不是 markdown
    里那一行——用 `harvest_caption` 重新收割会在两者不一致时报出一个库里并不存在的
    数字。"""
    index = element_positions.get(element_id)
    return bool(index is not None and elements[index].norm)


def _chunk_for_anchor(
    alignment: Alignment,
    position: int,
    chunk_by_element: Mapping[str, str],
) -> str:
    """锚点元素所属 chunk；不在任何 chunk 时沿对齐序有界回退，失败返回空串。

    调用方（`_anchor_for`）保证 ``position >= 0``。"""
    steps = 0
    while position >= 0 and steps <= CHUNK_LOOKBACK:
        chunk_id = chunk_by_element.get(alignment.matched[position])
        if chunk_id:
            return chunk_id
        position -= 1
        steps += 1
    return ""
