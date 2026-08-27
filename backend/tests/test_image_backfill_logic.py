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
    """元素与 markdown 完全对不上时，指针不前进、锚点保持 -1，图片被跳过而不是
    瞎插到某个不相干的元素后面。"""
    plan = _plan(
        "完全不相干的一段文字甲乙丙丁。\n\n![](images/a.jpg)\n",
        _els("另一份文档里的段落戊己庚辛。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"anchor_failed": 1}
    assert plan.coverage == 0.0


def test_document_leading_image_has_no_chunk_and_is_skipped():
    plan = _plan(
        "![](images/a.jpg)\n\n正文第一段在图片后面出现。\n",
        _els("正文第一段在图片后面出现。"),
        {f"el-{SID}-0001": "c1"},
        _index("a.jpg"),
    )
    assert plan.images == []
    assert plan.skipped == {"anchor_failed": 1}


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
        f"el-{SID}-0001-g01",
        f"el-{SID}-0001-g02",
        f"el-{SID}-0001-g03",
    ]


def test_new_ids_sort_between_their_anchor_and_the_next_element():
    """C collation 下 `-`(0x2D) < 数字，所以补出来的 id 落在锚点与下一条之间——
    元素顺序（分块与详情分页都按 id）因此保持文档序。"""
    ids = sorted([f"el-{SID}-0012", f"el-{SID}-0012-g01", f"el-{SID}-0013"])
    assert ids == [f"el-{SID}-0012", f"el-{SID}-0012-g01", f"el-{SID}-0013"]


def test_rerun_continues_the_suffix_instead_of_colliding():
    plan = _plan(
        "第一段正文写了一些内容。\n\n![](images/b.jpg)\n",
        _els("第一段正文写了一些内容。"),
        {f"el-{SID}-0001": "c1"},
        _index("b.jpg"),
        existing_element_ids=[f"el-{SID}-0001", f"el-{SID}-0001-g01"],
    )
    assert [image.element_id for image in plan.images] == [f"el-{SID}-0001-g02"]


def test_inline_image_anchors_on_its_own_line_element():
    markdown = (
        "第一段正文写了一些内容。\n\n"
        "第二段里内嵌了 ![](images/a.jpg) 这张图。\n"
    )
    plan = _plan(
        markdown,
        _els("第一段正文写了一些内容。", "第二段里内嵌了 这张图。"),
        {f"el-{SID}-0001": "c1", f"el-{SID}-0002": "c2"},
        _index("a.jpg"),
    )
    assert [image.anchor_element_id for image in plan.images] == [f"el-{SID}-0002"]
    assert [image.chunk_id for image in plan.images] == ["c2"]


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


def test_missing_output_root_is_tolerated(tmp_path):
    assert build_image_index([tmp_path / "nope"]).entries == {}
