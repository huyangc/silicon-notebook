"""`backfill-images` 纯逻辑层的聚焦用例：markdown 扫描、行分类、对齐、锚定、
图注收割、元素 id 续号。全部无数据库、无模型、无网络。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.image_backfill import (
    ElementView,
    ImageIndex,
    ImageIndexEntry,
    align,
    build_image_index,
    classify_target,
    harvest_caption,
    normalize_text,
    plan_source_images,
    scan_markdown,
)
from app.services.parsers import parse_markdown_text


SID = "s1"


def _els(*texts: str) -> list[ElementView]:
    return [
        ElementView(
            id=f"el-{SID}-{index:04d}",
            element_type="paragraph",
            norm=normalize_text(text),
        )
        for index, text in enumerate(texts, start=1)
    ]


def _parsed_els(markdown: str) -> list[ElementView]:
    """用**生产解析路径**产出元素，再按摄取路径的 `el-<sid>-NNNN` 形状铸 id。

    对齐算法的失败模式全部来自"元素侧长什么样"，而手搓的 `_els` 只会造段落——
    正是它让连续表格/连续带 alt 图片这两种真实漂移形态在原用例里一条都碰不到。
    `parse_markdown_text` 是 markdown 来源的真实产出口，`source_ingestion` 落库
    时按枚举序铸 `el-<source_id>-{index:04d}`。"""
    return [
        ElementView(
            id=f"el-{SID}-{index:04d}",
            element_type=element.element_type,
            norm=normalize_text(element.text),
        )
        for index, element in enumerate(parse_markdown_text(SID, markdown), start=1)
    ]


def _index(*names: str, size: int = 10) -> ImageIndex:
    return ImageIndex(
        entries={
            name: ImageIndexEntry(path=Path("/out") / "images" / name, size=size)
            for name in names
        }
    )


def _plan(markdown, elements, chunk_by_element, index, **kwargs):
    return plan_source_images(
        source_id=SID,
        markdown=markdown,
        elements=elements,
        existing_image_srcs=kwargs.pop("existing_image_srcs", []),
        existing_unassigned_srcs=kwargs.pop("existing_unassigned_srcs", {}),
        existing_element_ids=kwargs.pop(
            "existing_element_ids", [element.id for element in elements]
        ),
        chunk_by_element=chunk_by_element,
        image_index=index,
        max_images=kwargs.pop("max_images", 200),
        max_bytes=kwargs.pop("max_bytes", 5 * 1024 * 1024),
    )


# ------------------------------------------------------------------ 扫描/分类

def test_image_forms_are_all_recognised():
    _, refs = scan_markdown(
        "![a](images/a.jpg)\n"
        "![b](<images/b b.jpg>)\n"
        '![c](images/c.jpg "标题")\n'
        "![d](images/d.jpg?v=2#frag)\n"
    )
    assert [ref.src for ref in refs] == [
        "images/a.jpg",
        "images/b b.jpg",
        "images/c.jpg",
        "images/d.jpg",
    ]
    assert [ref.ordinal for ref in refs] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "target,expected",
    [
        ("images/a.jpg", "relative"),
        ("./a.png", "relative"),
        ("http://x/a.png", "remote"),
        ("HTTPS://x/a.png", "remote"),
        ("//cdn/a.png", "remote"),
        ("data:image/png;base64,AAA", "data"),
        ("DATA:image/png;base64,AAA", "data"),
    ],
)
def test_target_classification(target, expected):
    assert classify_target(target) == expected


def test_remote_and_data_uris_are_skipped_not_downloaded():
    plan = _plan(
        "正文一段足够长的话在这里。\n\n"
        "![](http://cdn/x.jpg)\n\n"
        "![](data:image/png;base64,AAAA)\n",
        _els("正文一段足够长的话在这里。"),
        {f"el-{SID}-0001": "c1"},
        _index("x.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"remote_uri": 1, "data_uri": 1}


def test_fenced_code_and_table_lines_never_match_elements():
    lines, _ = scan_markdown(
        "```python\nprint('这是一段代码不是正文')\n```\n| 甲 | 乙 |\n| - | - |\n"
    )
    assert [line.kind for line in lines] == [
        "code",
        "code",
        "code",
        "table",
        "table",
    ]


def test_tilde_fence_is_closed_by_its_own_marker():
    lines, _ = scan_markdown("~~~\n``` 不闭合\n~~~\n正文回到这里了对不对。\n")
    assert [line.kind for line in lines] == ["code", "code", "code", "text"]


# ------------------------------------------------------------------ 对齐

def test_alignment_is_monotone_and_ignores_non_text_lines():
    markdown = (
        "# 第一章标题在这里\n\n"
        "第一段正文写了一些内容。\n\n"
        "![](images/a.jpg)\n\n"
        "第二段正文继续往下说。\n"
    )
    elements = _els(
        "第一章标题在这里", "第一段正文写了一些内容。", "第二段正文继续往下说。"
    )
    lines, _ = scan_markdown(markdown)
    alignment = align(lines, elements)
    assert alignment.matched == [
        f"el-{SID}-0001",
        f"el-{SID}-0002",
        f"el-{SID}-0003",
    ]
    assert alignment.coverage == 1.0
    # 图片行（index 4）的锚点是它之前最后一个已匹配元素。
    assert alignment.position_by_line[4] == 1


def test_drifted_alignment_stops_matching_instead_of_guessing():
    """元素与 markdown 完全对不上时，指针不前进、覆盖率归零，整源被闸住而不是
    把图瞎插到某个不相干的元素后面。"""
    plan = _plan(
        "完全不相干的一段文字甲乙丙丁。\n\n![](images/a.jpg)\n",
        _els("另一份文档里的段落戊己庚辛。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"alignment_drifted": 1}
    assert plan.coverage == 0.0


def test_document_leading_image_has_no_anchor_and_is_skipped():
    """文档开头的图与"对齐失效"是两种处置完全不同的失败，reason code 分开：
    这里对齐是好的（cov=1.0），只是图前面一条元素都没有。"""
    plan = _plan(
        "![](images/a.jpg)\n\n正文第一段在图片后面出现。\n",
        _els("正文第一段在图片后面出现。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"no_anchor": 1}
    assert plan.coverage == 1.0


def test_anchor_without_chunk_walks_back_to_the_nearest_chunked_element():
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "第二段没有进任何切片里。\n\n"
        "![](images/a.jpg)\n"
    )
    plan = _plan(
        markdown,
        _els("第一段正文写了一些内容。", "第二段没有进任何切片里。"),
        {f"el-{SID}-0001": "c1"},  # 0002 不属于任何 chunk
        _index("a.jpg"),
    )
    assert [image.chunk_id for image in plan.images] == ["c1"]
    assert [image.anchor_element_id for image in plan.images] == [f"el-{SID}-0002"]


def test_no_chunk_at_all_is_accounted_separately_from_anchor_failure():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/a.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"no_chunk": 1}


# ------------------------------------------------------------------ 锚定与 id

def test_consecutive_images_share_one_anchor_and_get_increasing_suffixes():
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "![](images/a.jpg)\n\n"
        "![](images/b.jpg)\n\n"
        "![](images/c.jpg)\n"
    )
    plan = _plan(
        markdown,
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg", "b.jpg", "c.jpg"),
    )
    assert [image.element_id for image in plan.images] == [
        f"el-{SID}-0001-g001",
        f"el-{SID}-0001-g002",
        f"el-{SID}-0001-g003",
    ]


def test_minted_ids_stay_sorted_past_the_two_digit_boundary():
    """真断言：拿 id **铸造函数自己的产出**排一遍，而不是把三个手写字面量交给
    `sorted` 再断言它们有序（那只测了 Python 的 `sorted`）。

    每源上限是部署设置（默认 200），所以同一个锚点底下过百是可达的；两位位宽
    在第 100 张上会让 `"-g100" < "-g99"`，元素顺序当场乱掉。"""
    markdown = "第一段正文写了一些内容。\n\n" + "\n\n".join(
        f"![](images/x{index}.jpg)" for index in range(105)
    )
    plan = _plan(
        markdown,
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index(*[f"x{index}.jpg" for index in range(105)]),
    )
    ids = [image.element_id for image in plan.images]
    assert len(ids) == 105
    assert sorted(ids) == ids  # 铸出来的 id 本身就是单调的
    # 而且整段落在锚点与下一条元素之间（C collation 下 `-`(0x2D) < 数字）。
    assert sorted([f"el-{SID}-0001", *ids, f"el-{SID}-0002"]) == [
        f"el-{SID}-0001",
        *ids,
        f"el-{SID}-0002",
    ]


def test_a_suffix_beyond_the_fixed_width_is_skipped_not_minted_out_of_order():
    """位宽是固定的，而 `MINERU_MAX_IMAGES_PER_SOURCE` 是部署可配、没有上界校验
    的——两者在这一侧闭合：第 1000 张跳过并记账，而不是铸出 `-g1000`（C collation
    下 `"-g1000" < "-g999"`，这一锚点底下的元素顺序会当场乱掉）。"""
    markdown = "第一段正文写了一些内容。\n\n" + "\n\n".join(
        f"![](images/x{index}.jpg)" for index in range(1001)
    )
    plan = _plan(
        markdown,
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index(*[f"x{index}.jpg" for index in range(1001)]),
        max_images=5000,  # 每源上限刻意不设障，闸只能来自位宽
    )
    ids = [image.element_id for image in plan.images]
    assert len(ids) == 999
    assert ids[-1] == f"el-{SID}-0001-g999"
    assert sorted(ids) == ids  # 顺序仍单调
    assert plan.skipped == {"anchor_suffix_exhausted": 2}


def test_a_rerun_that_finds_the_suffix_already_exhausted_skips_immediately():
    """续号是从既有 id 扫出来的，所以上一趟用尽之后重跑同样必须跳过。"""
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/b.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("b.jpg"),
        existing_element_ids=[f"el-{SID}-0001", f"el-{SID}-0001-g999"],
    )
    assert plan.images == []
    assert plan.skipped == {"anchor_suffix_exhausted": 1}


def test_rerun_continues_the_suffix_instead_of_colliding():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/b.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("b.jpg"),
        existing_element_ids=[f"el-{SID}-0001", f"el-{SID}-0001-g001"],
    )
    assert [image.element_id for image in plan.images] == [f"el-{SID}-0001-g002"]


@pytest.mark.parametrize(
    "markdown",
    [
        "第一段正文写了一些内容。\n\n第二段里内嵌了 ![](images/a.jpg) 这张图。\n",
        "第一段正文写了一些内容。\n\n- 列表项里有 ![](images/a.jpg) 一张图\n",
        "第一段正文写了一些内容。\n\n| 甲 | ![](images/a.jpg) |\n| - | - |\n",
    ],
)
def test_images_that_do_not_own_their_line_are_skipped(markdown):
    """产品规则平移：在线 markdown 路径只对**独占一行**的图片落资产，列表项/
    表格单元格/段落中间的内嵌图片只留 alt 文本（`parse_markdown_text` 实测三种
    形态都不产出带 `metadata.src` 的 image 元素）。回填不得比在线路径更宽。"""
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"inline_image_skipped": 1}


def test_alignment_anchors_an_inline_image_line_on_its_own_element():
    """`position_by_line` 对**每一行**记账（不只整行图片），所以一条同时是正文
    又带内嵌图片的行，锚点是它自己而不是上一条。该图虽然按上面那条产品规则不
    回填，这条对齐性质仍然是后续行锚定正确的前提。"""
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "第二段里内嵌了 ![](images/a.jpg) 这张图。\n"
    )
    elements = _els("第一段正文写了一些内容。", "第二段里内嵌了 这张图。")
    lines, refs = scan_markdown(markdown)
    alignment = align(lines, elements)
    assert alignment.position_by_line[refs[0].line] == 1
    assert alignment.matched[1] == f"el-{SID}-0002"


# -------------------------------------------------- 对齐漂移（真实解析产出）

#: 两个漂移场景共用的收尾：目标图之前有一条正文段落，它才是正确锚点。
_TAIL = "\n\n第二段正文继续往下说明细节。\n\n![](images/z.jpg)\n\n第三段正文收尾了这一节。\n"


def _drift_plan(markdown: str):
    elements = _parsed_els(markdown)
    return elements, _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("z.jpg"),
    )


def test_a_run_of_tables_does_not_starve_the_lookahead_window():
    """8 张连续表格：markdown 侧每张是若干 `table` 行（不参与匹配），元素侧每张
    却是一条 `table` 元素。若前瞻预算对它们计数，8 张就把窗口（`ALIGN_LOOKAHEAD`
    = 8）吃干，指针从此**永久**停滞——此后每张图都静默锚到漂移点之前的元素上，
    `skipped` 里一个字都没有。修复前实测 cov=0.33、锚点落在 `el-s1-0001`。"""
    tables = "\n\n".join(
        f"| 甲{n} | 乙{n} |\n| --- | --- |\n| 丙{n} | 丁{n} |" for n in range(1, 9)
    )
    markdown = "第一段正文写了一些内容用于对齐锚定。\n\n" + tables + _TAIL
    elements, plan = _drift_plan(markdown)
    # paragraph, table ×8, paragraph, paragraph
    assert [element.element_type for element in elements] == (
        ["paragraph"] + ["table"] * 8 + ["paragraph", "paragraph"]
    )
    assert plan.coverage == 1.0
    assert [image.anchor_element_id for image in plan.images] == [f"el-{SID}-0010"]
    assert [image.src for image in plan.images] == ["images/z.jpg"]


def test_a_run_of_captioned_images_does_not_starve_the_lookahead_window():
    """9 张连续带 alt 的图片：解析路径给每张产出一条 `image` 元素（alt 当图注），
    而 markdown 侧那些行是 `image` 行、同样不参与匹配。与连续表格是同一个机制，
    修复前实测同样 cov=0.33、目标图锚到 `el-s1-0001`。"""
    gallery = "\n\n".join(
        f"![图 {n} 第 {n} 张示意图](images/p{n}.jpg)" for n in range(1, 10)
    )
    markdown = "第一段正文写了一些内容用于对齐锚定。\n\n" + gallery + _TAIL
    elements, plan = _drift_plan(markdown)
    assert [element.element_type for element in elements] == (
        ["paragraph"] + ["image"] * 9 + ["paragraph", "paragraph"]
    )
    assert plan.coverage == 1.0
    assert [image.anchor_element_id for image in plan.images] == [f"el-{SID}-0011"]
    assert [image.src for image in plan.images] == ["images/z.jpg"]
    # p1..p9 在索引里找不到（本用例只索引 z.jpg），照常逐张记账。
    assert plan.skipped == {"image_not_found": 9}


def test_an_inline_image_alt_survives_normalisation_so_its_line_still_matches():
    """在线解析路径对**内嵌**图片执行「只留 alt 文本、不落资产」，所以 alt 留在
    元素正文里（实测：``这一段里内嵌了 ![备注](x.jpg) 一张图。`` →
    ``这一段里内嵌了 备注 一张图。``）。扫描侧若把整段图片语法连 alt 一起抹掉，
    这一行就永远匹配不上它自己的元素——短文档里一行就能把覆盖率压到
    `MIN_ALIGNMENT_COVERAGE` 之下、让整源被 `alignment_drifted` 误跳（修复前实测
    cov=0.667），而独立成行的那张图会锚到错误的元素上（修复前锚到 0001）。"""
    markdown = (
        "第一段正文写了一些内容用于对齐锚定。\n\n"
        "这一段里内嵌了 ![备注](images/x.jpg) 一张图。\n\n"
        "![](images/z.jpg)\n\n"
        "第三段正文收尾了这一节。\n"
    )
    elements = _parsed_els(markdown)
    assert [element.element_type for element in elements] == ["paragraph"] * 3
    plan = _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("z.jpg"),
    )
    assert plan.coverage == 1.0
    assert plan.skipped.get("alignment_drifted") is None
    # 独立成行的那张图锚在它物理上紧跟的那条元素（内嵌图片那一段）之后。
    assert [image.src for image in plan.images] == ["images/z.jpg"]
    assert [image.anchor_element_id for image in plan.images] == [f"el-{SID}-0002"]
    # 内嵌那张仍按产品规则不落资产。
    assert plan.skipped["inline_image_skipped"] == 1


def test_an_image_only_line_stays_unmatched_even_when_it_carries_an_alt():
    """行**分类**必须用抹掉 alt 的那份归一化。独占一行的带图注图片在解析路径上
    产出的是一条 image 元素（不可匹配类型，指针无代价跨过），把这种行当成正文行
    会让它进覆盖率分母却永远匹配不上——9 张连续带图注的图片会把 cov 从 1.00 打到
    0.25，正好反向踩塌 `test_a_run_of_captioned_images_…` 守的那条闸。"""
    lines, _ = scan_markdown("![图 1 一张带图注的图](images/p1.jpg)\n")
    assert [line.kind for line in lines] == ["image"]
    assert lines[0].norm == ""


def test_crossed_elements_are_visible_to_the_chunk_walk_back():
    """跨过的不可匹配元素也进 `matched`：带图注的历史图片元素是**进过 chunk**
    的，它比更早那条段落离图更近，锚点回退理应先看见它。"""
    markdown = (
        "第一段正文写了一些内容用于对齐锚定。\n\n"
        "![图 1 一张历史图片](images/p1.jpg)\n\n"
        "第二段正文继续往下说明细节。\n\n"
        "![](images/z.jpg)\n"
    )
    elements = _parsed_els(markdown)
    # 只有那条历史图片元素属于 chunk，段落都不属于 → 回退必须走到它。
    plan = _plan(markdown, elements, {f"el-{SID}-0002": "c-img"}, _index("z.jpg"))
    assert [image.chunk_id for image in plan.images] == ["c-img"]


def test_a_stale_anchor_is_refused_rather_than_used():
    """对齐整体还过得去（覆盖率在闸上），但目标图之前连着一长串对不上的文本行
    ——那个锚点只是"最后一次还认得路的地方"。宁可不补，也不能插到错的位置。"""
    strays = "\n\n".join(f"另一份文档里的第 {n} 段落戊己庚辛。" for n in range(1, 11))
    matched = "\n\n".join(f"能对上的第 {n} 段正文内容在这里。" for n in range(1, 41))
    markdown = matched + "\n\n" + strays + "\n\n![](images/z.jpg)\n"
    elements = _els(*[f"能对上的第 {n} 段正文内容在这里。" for n in range(1, 41)])
    plan = _plan(
        markdown, elements, {element.id: "c1" for element in elements}, _index("z.jpg")
    )
    assert plan.coverage >= 0.8  # 整源闸没有触发
    assert plan.images == []
    assert plan.skipped == {"anchor_stale": 1}


def test_a_source_below_the_coverage_floor_is_skipped_whole():
    """整源闸：对齐可信度低于 `MIN_ALIGNMENT_COVERAGE` 时一张图都不补，并以
    `alignment_drifted` 逐张记账（dry-run / report 照常看得见）。"""
    markdown = (
        "\n\n".join(f"对不上的第 {n} 段文字甲乙丙丁。" for n in range(1, 11))
        + "\n\n![](images/z.jpg)\n\n![](images/y.jpg)\n"
    )
    elements = _els("只有这一段能对上的正文内容。")
    plan = _plan(
        markdown,
        elements,
        {f"el-{SID}-0001": "c1"},
        _index("z.jpg", "y.jpg"),
    )
    assert plan.coverage < 0.8
    assert plan.images == []
    assert plan.skipped == {"alignment_drifted": 2}


# -------------------------------------------------- 既有无资产图片的就地补齐

def test_an_existing_captioned_image_is_enriched_not_duplicated():
    """带 alt 的相对路径图片，解析路径已经产出过一条 image 元素（有 `src`、
    没有 `asset_id`）。它不在"已补过"集合里（那条判据要求 asset_id 非空），
    按"只插入"处理就会给同一张图造出第二条元素行。"""
    markdown = "第一段正文写了一些内容。\n\n![图 1 系统架构](images/a.jpg)\n"
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {f"el-{SID}-0001": "c1", f"el-{SID}-0002": "c1"},
        _index("a.jpg"),
        existing_unassigned_srcs={"images/a.jpg": f"el-{SID}-0002"},
    )
    assert plan.images == []  # 一条新元素都不插
    assert [item.element_id for item in plan.enriched] == [f"el-{SID}-0002"]
    # 它已经在 chunk 里 → chunk 零改动。
    assert [item.chunk_id for item in plan.enriched] == [""]
    assert plan.skipped == {}


def test_an_existing_image_outside_every_chunk_joins_its_anchor_chunk():
    """另一种子形态：既有元素不属于任何 chunk（历史行不保证进过分块）。这时按
    与新插入同款的锚点路径把它 append 进锚点 chunk。"""
    markdown = "第一段正文写了一些内容。\n\n![图 1 系统架构](images/a.jpg)\n"
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {f"el-{SID}-0001": "c1"},  # 0002 不在任何 chunk 里
        _index("a.jpg"),
        existing_unassigned_srcs={"images/a.jpg": f"el-{SID}-0002"},
    )
    assert plan.images == []
    assert [item.chunk_id for item in plan.enriched] == ["c1"]


def test_enrichment_does_not_consume_the_per_source_cap():
    """就地补齐不新增元素行，所以不该吃每源张数预算——它补的那条元素本来就已经
    计在既有 image 元素数里了。"""
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "![图 1 系统架构](images/a.jpg)\n\n"
        "![](images/b.jpg)\n"
    )
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("a.jpg", "b.jpg"),
        existing_unassigned_srcs={"images/a.jpg": f"el-{SID}-0002"},
        max_images=2,  # 既有 1 张 image 元素 → 还剩 1 个新增名额
    )
    assert [item.src for item in plan.enriched] == ["images/a.jpg"]
    assert [image.src for image in plan.images] == ["images/b.jpg"]
    assert "per_source_cap" not in plan.skipped


def test_the_same_src_referenced_twice_is_enriched_once():
    """一个 src 在文档里被引用两次，但只有一条既有元素可补。补两遍会把同一个
    element id append 进 chunk 两次（`element_ids` 里出现重复），而"再插一条新
    元素"等于凭空造出一条在线路径不会有的行。"""
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "![图 1 系统架构](images/a.jpg)\n\n"
        "第二段又引了同一张图。\n\n"
        "![图 1 系统架构](images/a.jpg)\n"
    )
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("a.jpg"),
        existing_unassigned_srcs={"images/a.jpg": f"el-{SID}-0002"},
    )
    assert [item.element_id for item in plan.enriched] == [f"el-{SID}-0002"]
    assert plan.images == []
    assert plan.skipped == {"duplicate_src_reference": 1}


def test_an_already_assigned_src_wins_over_enrichment():
    """幂等：补齐过一轮之后 `asset_id` 非空，下一轮它落回"已补过"那一支，既不
    重复插入也不重复补齐。"""
    markdown = "第一段正文写了一些内容。\n\n![图 1 系统架构](images/a.jpg)\n"
    elements = _parsed_els(markdown)
    plan = _plan(
        markdown,
        elements,
        {element.id: "c1" for element in elements},
        _index("a.jpg"),
        existing_image_srcs=["images/a.jpg"],
        existing_unassigned_srcs={},
    )
    assert plan.images == []
    assert plan.enriched == []
    assert plan.skipped == {"already_backfilled": 1}


# ------------------------------------------------------------------ 图注

def test_caption_is_harvested_from_the_line_below_the_image():
    lines, _ = scan_markdown("![](images/a.jpg)\n\n图 1 系统总体架构\n")
    assert harvest_caption(lines, 0) == "图 1 系统总体架构"


def test_caption_is_harvested_from_the_line_above_when_below_has_none():
    lines, _ = scan_markdown("Figure 2. Pipeline overview\n\n![](images/a.jpg)\n")
    assert harvest_caption(lines, 2) == "Figure 2. Pipeline overview"


def test_ordinary_prose_next_to_an_image_is_not_taken_as_a_caption():
    lines, _ = scan_markdown("![](images/a.jpg)\n\n这只是普通的一段正文而已。\n")
    assert harvest_caption(lines, 0) == ""


def test_caption_scan_does_not_cross_another_image():
    lines, _ = scan_markdown("![](images/a.jpg)\n![](images/b.jpg)\n图 3 第二张图\n")
    assert harvest_caption(lines, 0) == ""
    assert harvest_caption(lines, 1) == "图 3 第二张图"


def test_alt_text_is_the_caption_fallback():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![流程示意](images/a.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
    )
    assert [image.caption for image in plan.images] == ["流程示意"]
    assert plan.captions == 1


# ------------------------------------------------------------------ 幂等/上限

def test_already_backfilled_srcs_are_skipped_so_reruns_are_incremental():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/a.jpg)\n\n![](images/b.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg", "b.jpg"),
        existing_image_srcs=["images/a.jpg"],
    )
    assert [image.src for image in plan.images] == ["images/b.jpg"]
    assert plan.skipped == {"already_backfilled": 1}


def test_missing_images_are_accounted_and_the_rest_still_backfill():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/gone.jpg)\n\n![](images/here.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("here.jpg"),
    )
    assert [image.src for image in plan.images] == ["images/here.jpg"]
    assert plan.skipped == {"image_not_found": 1}


def test_per_source_cap_truncates_in_markdown_order_and_is_accounted():
    plan = _plan(
        "第一段正文写了一些内容。\n\n"
        "![](images/a.jpg)\n\n![](images/b.jpg)\n\n![](images/c.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg", "b.jpg", "c.jpg"),
        max_images=2,
    )
    assert [image.src for image in plan.images] == ["images/a.jpg", "images/b.jpg"]
    assert plan.skipped == {"per_source_cap": 1}


def test_existing_image_elements_count_against_the_per_source_cap():
    elements = _els("第一段正文写了一些内容。")
    elements.append(ElementView(id=f"el-{SID}-0002", element_type="image", norm=""))
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/a.jpg)\n",
        elements,
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
        max_images=1,
    )
    assert plan.images == []
    assert plan.skipped == {"per_source_cap": 1}


def test_oversized_images_are_skipped():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/a.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg", size=999),
        max_bytes=100,
    )
    assert plan.images == []
    assert plan.skipped == {"image_too_large": 1}


# ------------------------------------------------------------------ 图片索引

def test_index_only_accepts_files_directly_under_an_images_directory(tmp_path):
    (tmp_path / "sess" / "doc" / "auto" / "images").mkdir(parents=True)
    (tmp_path / "sess" / "doc" / "auto" / "images" / "a.jpg").write_bytes(b"x" * 3)
    (tmp_path / "sess" / "doc" / "ocr" / "images").mkdir(parents=True)
    (tmp_path / "sess" / "doc" / "ocr" / "images" / "b.jpg").write_bytes(b"yy")
    (tmp_path / "sess" / "doc" / "layout.png").write_bytes(b"z")
    index = build_image_index([tmp_path])
    assert set(index.entries) == {"a.jpg", "b.jpg"}
    assert index.get("a.jpg").size == 3


def test_same_name_different_size_keeps_the_first_and_warns(tmp_path):
    for method, payload in (("auto", b"x"), ("ocr", b"xxxx")):
        directory = tmp_path / method / "images"
        directory.mkdir(parents=True)
        (directory / "dup.jpg").write_bytes(payload)
    index = build_image_index([tmp_path])
    assert index.get("dup.jpg").size == 1  # auto 先被遍历到
    assert index.duplicates == ["dup.jpg"]


def test_one_missing_root_among_several_is_tolerated(tmp_path):
    """索引本身对缺失 root 保持容忍（多路径运行里一条挂载点没上来不该废掉整
    跑）。"全部 root 都缺失 / 索引为空"是运行前置条件，由阶段层拒绝——见
    `test_image_backfill_phase.py` 的两条 refuse 用例。"""
    good = tmp_path / "sess" / "doc" / "auto" / "images"
    good.mkdir(parents=True)
    (good / "a.jpg").write_bytes(b"x")
    index = build_image_index([tmp_path / "nope", tmp_path])
    assert set(index.entries) == {"a.jpg"}
    assert build_image_index([tmp_path / "nope"]).entries == {}


def test_index_walk_is_deterministic_regardless_of_directory_creation_order(tmp_path):
    """逐目录 `os.walk` + 排序给出的顺序必须与"整树物化再排序"一致：目录字典序
    在先、目录内文件字典序在后，"同名先见者取"因此逐字不变（这里 `a/` 先于
    `b/`，所以 1 字节那份胜出，与文件系统返回的顺序无关）。"""
    for method, payload in (("b", b"xxxx"), ("a", b"x")):
        directory = tmp_path / method / "images"
        directory.mkdir(parents=True)
        (directory / "dup.jpg").write_bytes(payload)
    index = build_image_index([tmp_path])
    assert index.get("dup.jpg").size == 1
    assert index.get("dup.jpg").path.parent.parent.name == "a"
    assert index.duplicates == ["dup.jpg"]
