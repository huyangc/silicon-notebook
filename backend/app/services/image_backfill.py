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

    line_exclusive: bool
    """这一行除了图片引用（与空白）之外别无他物。

    在线 markdown 路径只把**独占一整行**的图片产出成 image 元素并落资产；列表
    项、表格单元格与段落中间的内嵌图片一律只留 alt 文本
    （`parse_markdown_text` 实测：``- 列表项 ![alt](b.jpg)`` → ``list_item``、
    ``| 甲 | ![alt](c.jpg) |`` → ``table``、``见 ![](a.jpg) 所示`` →
    ``paragraph``，三者都没有 ``metadata.src``）。回填必须服从同一条产品规则，
    否则它会造出一批在线路径永远不会产出的资产。"""


@dataclass(frozen=True)
class MarkdownLine:
    index: int
    raw: str
    kind: str
    """``"text"`` / ``"image"`` / ``"table"`` / ``"code"`` / ``"blank"``。"""

    norm: str
    """归一化后的可比较文本（仅 ``kind == "text"`` 有意义）。"""


def normalize_text(value: str) -> str:
    """把 markdown 行与元素文本折成同一种可比较形状。

    两侧必须逐字共用这一个函数：分别写两份归一化是这类对齐算法最典型的静默
    失败——它不报错，只是永远匹配不上，于是每张图都"锚定失败"被跳过。"""
    text = _LINE_DECORATION_RE.sub("", value or "")
    text = _IMAGE_RE.sub(" ", text)
    text = _INLINE_MARKS_RE.sub("", text)
    return " ".join(text.split())


def _strip_target(raw: str) -> str:
    target = (raw or "").strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    # query/fragment 是 URL 语法，不属于存下来的文件名（与 `_bundle_image_path`
    # 同款处理）。
    for sep in ("#", "?"):
        cut = target.find(sep)
        if cut >= 0:
            target = target[:cut]
    return target.strip()


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
    表折成一条元素，逐行去匹配只会把指针推过头）。"""
    lines: list[MarkdownLine] = []
    refs: list[ImageRef] = []
    fence: str | None = None
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
            continue
        if fence_match:
            fence = fence_match.group("fence")
            lines.append(MarkdownLine(index, raw, "code", ""))
            continue

        # 「这一行只有图片」按**原始**行判：剥掉全部图片引用后还剩下什么。刻意
        # 不复用 `normalize_text`——它会先剥掉行首的列表/引用/标题标记，于是
        # `- ![](b.jpg)` 会被判成"只有图片"，而在线路径对它产出的是 list_item。
        line_exclusive = not _IMAGE_RE.sub("", raw).strip()
        line_refs: list[ImageRef] = []
        for match in _IMAGE_RE.finditer(raw):
            ordinal += 1
            target = _strip_target(match.group("target"))
            line_refs.append(
                ImageRef(
                    line=index,
                    ordinal=ordinal,
                    alt=(match.group("alt") or "").strip(),
                    src=target,
                    kind=classify_target(target),
                    line_exclusive=line_exclusive,
                )
            )
        refs.extend(line_refs)

        stripped = raw.strip()
        if not stripped:
            kind = "blank"
        elif line_refs and not normalize_text(raw):
            # 整行只有图片（剥掉图片语法后什么都不剩）：它是锚定的定位点，不是
            # 可匹配的正文行。
            kind = "image"
        elif stripped.startswith("|"):
            kind = "table"
        else:
            kind = "text"
        lines.append(
            MarkdownLine(index, raw, kind, normalize_text(raw) if kind == "text" else "")
        )
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
    """既有元素在对齐里用到的那一小部分。"""

    id: str
    element_type: str
    norm: str


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
    for line in lines:
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

    remaining = max_images - sum(
        1 for element in elements if element.element_type == "image"
    )
    for ref in refs:
        if ref.kind != "relative":
            plan.skip(f"{ref.kind}_uri")
            continue
        if not ref.line_exclusive:
            # 产品规则：列表项/表格单元格/段落中间的内嵌图片只留 alt 文本、
            # 不落资产（见 `ImageRef.line_exclusive`）。MinerU 的主流形态是图片
            # 独占一行，不受影响。
            plan.skip("inline_image_skipped")
            continue
        if ref.src in already:
            plan.skip("already_backfilled")
            continue
        if drifted:
            # 整源闸：对齐已经不可信，每个锚点都是猜的（见
            # `MIN_ALIGNMENT_COVERAGE`）。这里逐张记账而不是整源记一条，是为了
            # 让 stdout 汇总里的 `alignment_drifted` 与其它 reason code 同口径
            # （都是"多少张图没补上"）。
            plan.skip("alignment_drifted")
            continue
        entry = image_index.get(Path(ref.src).name)
        if entry is None:
            plan.skip("image_not_found")
            continue
        if entry.size > max_bytes:
            plan.skip("image_too_large")
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
            # 不在（无图注的历史元素不进 chunk）才走与新插入同款的锚点路径。
            if chunk_by_element.get(existing_id):
                chunk_id = ""
            else:
                anchor = _anchor_for(plan, alignment, ref, chunk_by_element)
                if anchor is None:
                    continue
                chunk_id = anchor[1]
            enriched_ids.add(existing_id)
            plan.enriched.append(
                EnrichedImage(
                    element_id=existing_id,
                    src=ref.src,
                    source_path=entry.path,
                    size=entry.size,
                    chunk_id=chunk_id,
                )
            )
            continue

        if remaining <= 0:
            plan.skip("per_source_cap")
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
