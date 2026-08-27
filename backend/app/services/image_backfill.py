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

#: 锚点元素不在任何 chunk 时，沿对齐序向前回退的最大步数。
CHUNK_LOOKBACK = 12

#: 图注收割时，向图片行前后各看几行。
CAPTION_SCAN_LINES = 3

#: 前缀匹配至少要有这么多字符才算数，避免"第一章"匹配上任意以它开头的段落。
MIN_PREFIX_MATCH_CHARS = 12

#: 文档开头没有任何已匹配元素时使用的合成锚点后缀（`el-<sid>-0000` 排在
#: `el-<sid>-0001` 之前）。注意：合成基座本身永远不属于任何 chunk，所以锚在它上
#: 面的图片会在 chunk 归属这一步被记账跳过——这是刻意的，一个进不了任何 chunk
#: 的图片元素永远不会被引用到，写进去只是死行。
BASE_ANCHOR_ORDINAL = "0000"


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
    index = ImageIndex()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.parent.name != "images":
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            existing = index.entries.get(path.name)
            if existing is not None:
                if existing.size != size and path.name not in index.duplicates:
                    index.duplicates.append(path.name)
                continue
            index.entries[path.name] = ImageIndexEntry(path=path, size=size)
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

    matched: list[str]
    """按对齐顺序真正匹配上的元素 id。"""

    matched_lines: int
    text_lines: int

    @property
    def coverage(self) -> float:
        return (self.matched_lines / self.text_lines) if self.text_lines else 0.0


def align(lines: Sequence[MarkdownLine], elements: Sequence[ElementView]) -> Alignment:
    """markdown 行与既有元素的单调双指针对齐。

    单调是关键：元素 id 字典序就是文档序（`el-<sid>-NNNN` 零补位，`chunking`
    的读取按 ``ORDER BY id``），所以指针只前进不回退，一旦对齐漂掉就自然停止
    匹配，而不是在文档后半段乱认。"""
    position_by_line: dict[int, int] = {}
    matched: list[str] = []
    cursor = 0
    matched_lines = 0
    text_lines = 0
    for line in lines:
        if line.kind == "text":
            text_lines += 1
            limit = min(cursor + ALIGN_LOOKAHEAD, len(elements))
            for j in range(cursor, limit):
                if _matches(line.norm, elements[j].norm):
                    matched.append(elements[j].id)
                    cursor = j + 1
                    matched_lines += 1
                    break
        position_by_line[line.index] = len(matched) - 1
    return Alignment(
        position_by_line=position_by_line,
        matched=matched,
        matched_lines=matched_lines,
        text_lines=text_lines,
    )


def harvest_caption(lines: Sequence[MarkdownLine], image_line: int) -> str:
    """图片行前后最近的非空行里机会性地取一条图注。

    先看下方再看上方（MinerU 的版面还原把图注排在图下是常态），不跨越另一张
    图，最多各看 ``CAPTION_SCAN_LINES`` 行。收不到就空着——显示不依赖图注。"""
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


@dataclass
class SourcePlan:
    source_id: str
    images: list[PlannedImage] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    """稳定 reason code -> 计数。绝不含图片字节或正文。"""

    captions: int = 0
    coverage: float = 0.0

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + count


def _anchor_element_id(source_id: str, alignment: Alignment, position: int) -> str:
    if position < 0:
        return f"el-{source_id}-{BASE_ANCHOR_ORDINAL}"
    return alignment.matched[position]


def plan_source_images(
    *,
    source_id: str,
    markdown: str,
    elements: Sequence[ElementView],
    existing_image_srcs: Iterable[str],
    existing_element_ids: Iterable[str],
    chunk_by_element: Mapping[str, str],
    image_index: ImageIndex,
    max_images: int,
    max_bytes: int,
) -> SourcePlan:
    """算出这个来源应该补哪些图片元素、每张挂在哪个 chunk 上。

    纯函数：不读数据库、不写盘。``existing_image_srcs`` 是本来源已有的、带
    ``metadata.asset_id`` 的图片元素的 ``metadata.src`` 集合——它就是重跑的增量
    判据（找回更多原图之后再跑一次只会补新的，已补的一张都不会重写）。
    """
    plan = SourcePlan(source_id=source_id)
    lines, refs = scan_markdown(markdown)
    alignment = align(lines, elements)
    plan.coverage = alignment.coverage

    already = {src for src in existing_image_srcs if src}
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
            plan.skip("per_source_cap")
            continue

        position = alignment.position_by_line.get(ref.line, -1)
        anchor_id = _anchor_element_id(source_id, alignment, position)

        chunk_id, walked = _chunk_for_anchor(alignment, position, chunk_by_element)
        if not chunk_id:
            plan.skip("no_chunk" if walked else "anchor_failed")
            continue

        used_suffix[anchor_id] = used_suffix.get(anchor_id, 0) + 1
        caption = harvest_caption(lines, ref.line) or ref.alt
        if caption:
            plan.captions += 1
        plan.images.append(
            PlannedImage(
                element_id=f"{anchor_id}-g{used_suffix[anchor_id]:02d}",
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


def _chunk_for_anchor(
    alignment: Alignment,
    position: int,
    chunk_by_element: Mapping[str, str],
) -> tuple[str, bool]:
    """锚点元素所属 chunk；不在任何 chunk 时沿对齐序有界回退。

    返回 ``(chunk_id, walked)``：``walked`` 为真表示确实找到过锚点、只是它（和
    它之前有界范围内的元素）都不属于任何 chunk——这两种失败在记账里要分开，
    "对齐彻底漂掉"和"这段文字本来就没进检索"是不同的运维结论。"""
    if position < 0:
        return "", False
    steps = 0
    while position >= 0 and steps <= CHUNK_LOOKBACK:
        chunk_id = chunk_by_element.get(alignment.matched[position])
        if chunk_id:
            return chunk_id, True
        position -= 1
        steps += 1
    return "", True
