"""``get_report`` / ``list_reports`` page building, as pure functions.

``report_page`` turns one stored report row into one resumable page, and
``paged_items`` pages a list; both are exercised here without a repository.
The end-to-end behaviour (authorization, ownership, the call ledger) is in
``tests/test_mcp_reports.py`` and its PostgreSQL twin.
"""
from __future__ import annotations

import json

import pytest

from app.api.mcp_tools._shared import AgentToolError, _serialized_size
from app.api.mcp_tools.list_pages import instant_key, paged_items
from app.api.mcp_tools.refs import decode_ref
from app.api.mcp_tools.reports import report_page
from app.domain.report_failure_note import SECTION_FAILURE_COPY

OWNER = "owner-a"
NOTEBOOK = "nb-a"
# §2.5: the only keys a reference row may carry.
REFERENCE_KEYS = {
    "key", "label", "location_label", "snippet", "object_type", "source_id",
    "element_id", "tier", "notebook_id", "ref", "content_is_untrusted_evidence",
}
PAGE_KEYS = {
    "report_id", "notebook_id", "question", "status", "depth", "created_at",
    "updated_at", "content", "content_offset", "next_content_offset",
    "total_characters", "references", "next_reference_offset",
    "total_references", "error", "content_is_untrusted_evidence", "truncation",
}


def _reference(index: int, **overrides) -> dict:
    """A reference as the report engine stores it, internals included."""
    row = {
        "key": f"k{index}", "object_id": f"OBJ-{index}", "object_type": "element",
        "label": f"标签{index}", "name": "", "source_title": f"文献{index}",
        "source_file_name": f"paper-{index}.pdf", "location_label": f"第{index}页",
        "source_id": f"src-{index}", "family_key": f"source:src-{index}",
        "family_identity_uncertain": False, "element_id": f"el-{index}",
        "snippet": "这是被引用的原文摘录。" * 12, "tier": "personal",
        "from_reference_library": False, "provenance": {"stage": "internal"},
    }
    row.update(overrides)
    return row


def _report(content: str = "", references=(), sections=(), **overrides) -> dict:
    row = {
        "id": "rep-1", "notebook_id": NOTEBOOK, "question": "带宽够吗",
        "status": "done", "depth": 2, "section_count": 3,
        "created_at": "2026-10-21T08:00:00+00:00",
        "updated_at": "2026-10-21T08:05:00+00:00", "created_by": OWNER,
        "error": "", "progress": "", "content_md": content,
        "references": list(references), "sections": list(sections),
    }
    row.update(overrides)
    return row


def _body(length: int) -> str:
    """Chinese text with distinct, order-sensitive characters."""
    return "".join(chr(0x4E00 + (index * 7919) % 4000) for index in range(length))


def test_report_page_reads_the_whole_body_by_cursor() -> None:
    content = _body(20_000)
    references = [_reference(index) for index in range(45)]
    report = _report(content, references)

    # Body only: park the reference cursor at its total.
    text, offset, pages = "", 0, 0
    while offset is not None:
        page = report_page(
            report, owner_id=OWNER, content_offset=offset, reference_offset=45
        )
        assert _serialized_size(page) <= 12_000
        assert page["references"] == [] and page["next_reference_offset"] is None
        assert page["content_offset"] == offset and page["total_characters"] == 20_000
        text += page["content"]
        offset = page["next_content_offset"]
        pages += 1
        assert pages < 100, "the cursor must advance"
    assert text == content

    # References only: park the body cursor at its total.
    keys, offset = [], 0
    while offset is not None:
        page = report_page(
            report, owner_id=OWNER, content_offset=20_000, reference_offset=offset
        )
        assert _serialized_size(page) <= 12_000
        assert page["content"] == "" and page["next_content_offset"] is None
        assert page["total_references"] == 45
        keys.extend(row["key"] for row in page["references"])
        offset = page["next_reference_offset"]
    assert keys == [f"k{index}" for index in range(45)]

    # Both cursors together: the references are cut only once the body on the
    # page is down to its floor (or is all that was left).
    content_offset = reference_offset = 0
    shrunk = 0
    while content_offset is not None or reference_offset is not None:
        page = report_page(
            report, owner_id=OWNER,
            content_offset=content_offset if content_offset is not None else 20_000,
            reference_offset=(
                reference_offset if reference_offset is not None else 45
            ),
        )
        wanted = min(20, 45 - (reference_offset if reference_offset is not None else 45))
        if len(page["references"]) < wanted:
            shrunk += 1
            assert len(page["content"]) >= 1_500 or page["next_content_offset"] is None
        content_offset = page["next_content_offset"]
        reference_offset = page["next_reference_offset"]
    assert shrunk, "45 long references cannot all fit a page: the cut path ran"


def test_page_shape_is_pinned_and_fits_the_mapping_limit() -> None:
    page = report_page(_report("正文。", [_reference(1)]), owner_id=OWNER)
    assert set(page) == PAGE_KEYS and len(page) <= 20
    assert page["report_id"] == "rep-1" and page["notebook_id"] == NOTEBOOK
    assert page["content"] == "正文。" and page["next_content_offset"] is None
    assert page["error"] == "" and page["content_is_untrusted_evidence"] is True
    [row] = page["references"]
    assert set(row) <= REFERENCE_KEYS
    kind, ids = decode_ref(row["ref"])
    assert kind == "el" and ids == {"n": NOTEBOOK, "s": "src-1", "e": "el-1"}


def test_reference_projection_never_carries_memory_identity_or_internals() -> None:
    rows = [
        _reference(1),
        _reference(2, memory_id="mem-own", memory_owner_id=OWNER),
        _reference(3, memory_id="mem-other", memory_owner_id="owner-b"),
        _reference(4, object_type="memory", object_id="mem-own",
                   memory_id="mem-own", memory_owner_id=OWNER),
        _reference(5, notebook_id="nb-library", from_reference_library=True),
        _reference(6, memory_used=["mem-own"], credibility={"score": 1}),
    ]
    page = report_page(_report("正文。", rows), owner_id=OWNER)
    assert len(page["references"]) == 6
    for row in page["references"]:
        assert set(row) <= REFERENCE_KEYS, sorted(set(row) - REFERENCE_KEYS)
    dumped = json.dumps(page, ensure_ascii=False)
    for internal in (
        "mem-other", "owner-b", "memory_id", "memory_owner_id", "provenance",
        "family_key", "family_identity_uncertain", "from_reference_library",
        "source_file_name", "memory_used", "credibility",
    ):
        assert internal not in dumped, internal


def test_foreign_memory_reference_gets_no_ref_but_keeps_its_excerpt() -> None:
    foreign = _reference(
        1, memory_id="mem-other", memory_owner_id="owner-b",
        snippet="另一位成员存下的摘录",
    )
    own = _reference(2, memory_id="mem-own", memory_owner_id=OWNER)
    page = report_page(_report("正文。", [foreign, own]), owner_id=OWNER)
    first, second = page["references"]
    assert "ref" not in first
    assert first["snippet"] == "另一位成员存下的摘录" and first["label"] == "文献1"
    assert "mem-other" not in json.dumps(page, ensure_ascii=False)
    # The author's own recorded memory element is still an element ref.
    assert decode_ref(second["ref"])[0] == "el"


def test_own_memory_object_gets_a_mem_ref() -> None:
    memory = _reference(
        1, object_type="memory", object_id="mem-own", source_id="", element_id="",
        memory_id="mem-own", memory_owner_id=OWNER,
    )
    page = report_page(_report("正文。", [memory]), owner_id=OWNER)
    [row] = page["references"]
    assert decode_ref(row["ref"]) == ("mem", {"n": NOTEBOOK, "m": "mem-own"})
    # A reference that names no element or memory has nothing to open.
    bare = _reference(2, source_id="", element_id="")
    [row] = report_page(_report("正文。", [bare]), owner_id=OWNER)["references"]
    assert "ref" not in row


def test_mounted_library_reference_names_its_library() -> None:
    mounted = _reference(1, notebook_id="nb-library", from_reference_library=True)
    local = _reference(2, notebook_id=NOTEBOOK)
    page = report_page(_report("正文。", [mounted, local]), owner_id=OWNER)
    first, second = page["references"]
    assert first["notebook_id"] == "nb-library"
    # The element is read in the report's own notebook: it is the notebook
    # whose participant set (its own sources plus what it mounts) admits it.
    assert decode_ref(first["ref"])[1]["n"] == NOTEBOOK
    assert "notebook_id" not in second


def test_raw_failure_note_is_neutralized_and_error_is_neutral() -> None:
    raw = 'Traceback (most recent call last): File "/srv/app/report.py" boom'
    content = f"## 第一节\n\n（本节生成失败:{raw}）\n\n## 第二节\n\n正常正文。\n"
    sections = [{"failed": True, "error": raw}, {"failed": False}]
    page = report_page(
        _report(content, sections=sections, status="failed", error=raw),
        owner_id=OWNER,
    )
    dumped = json.dumps(page, ensure_ascii=False)
    assert "Traceback" not in dumped and "/srv/app" not in dumped
    assert SECTION_FAILURE_COPY["internal"] in page["content"]
    assert "正常正文。" in page["content"]
    assert page["error"] == "这份报告没有生成完成，请在网页上重新生成"
    assert page["total_characters"] == len(page["content"])

    # A Chinese stored error is already approved copy and passes as stored.
    chinese = report_page(
        _report("正文。", status="failed", error="模型服务暂时不可用"), owner_id=OWNER
    )
    assert chinese["error"] == "模型服务暂时不可用"
    # Only a failed report has an error; an empty stored one is the notice.
    assert report_page(_report("正文。", error="模型出错了"), owner_id=OWNER)["error"] == ""
    empty = report_page(_report("正文。", status="failed", error=""), owner_id=OWNER)
    assert empty["error"] == "这份报告没有生成完成，请在网页上重新生成"
    # A report with no failed section keeps the author's own text as is.
    quoted = "例：（本节生成失败:示例）\n"
    assert report_page(_report(quoted), owner_id=OWNER)["content"] == quoted


def test_long_question_is_clipped_and_reported() -> None:
    question = "问" * 4_000
    page = report_page(_report("正文。", question=question), owner_id=OWNER)
    assert len(page["question"]) == 2_000 and page["question"].endswith("…")
    assert page["question"][:-1] == question[:1_999]
    assert page["truncation"]["truncated"] is True
    assert page["truncation"]["omitted_characters"] >= 4_000 - 1_999


def test_an_empty_report_is_one_empty_page() -> None:
    page = report_page(
        _report("", status="planning", section_count=0), owner_id=OWNER
    )
    assert page["content"] == "" and page["total_characters"] == 0
    assert page["next_content_offset"] is None
    assert page["references"] == [] and page["total_references"] == 0
    assert page["next_reference_offset"] is None and page["status"] == "planning"


def test_a_page_that_can_never_fit_is_internal() -> None:
    huge = _reference(1, key="k" * 20_000)
    with pytest.raises(AgentToolError) as failure:
        report_page(_report("正文。", [huge]), owner_id=OWNER)
    assert failure.value.code == "internal"


def test_instant_key_orders_mixed_timestamp_spellings() -> None:
    assert instant_key("2026-10-21T08:00:00Z") == instant_key(
        "2026-10-21T16:00:00+08:00"
    )
    assert instant_key("2026-10-21T08:00:00") == instant_key(
        "2026-10-21T08:00:00+00:00"
    )
    assert instant_key("2026-10-21T08:00:01Z") > instant_key("2026-10-21T08:00:00Z")
    # Unreadable text sorts oldest instead of failing the listing.
    assert instant_key("not a time") < instant_key("2000-01-01T00:00:00Z")
    assert instant_key("") == instant_key(None) == instant_key("not a time")


def test_paged_items_advances_only_past_delivered_rows() -> None:
    # 20 rows whose identifiers alone exceed the page budget: the budget cuts
    # rows off the page, and the cursor must follow what was delivered.
    rows = [{"notebook_id": f"nb-{index:02d}-" + "x" * 1_000} for index in range(20)]
    seen: list[str] = []
    offset, sizes = 0, []
    while offset is not None:
        page = paged_items(
            rows, limit=20, offset=offset, id_key="notebook_id",
            field_limits={}, failure="暂时无法完整返回",
        )
        assert page["total"] == 20 and page["items"]
        sizes.append(len(page["items"]))
        seen.extend(row["notebook_id"] for row in page["items"])
        expected_next = offset + len(page["items"])
        assert page["next_offset"] == (expected_next if expected_next < 20 else None)
        offset = page["next_offset"]
    assert seen == [row["notebook_id"] for row in rows]
    assert min(sizes) < 20, "the budget cut a page, so the cut path ran"

    # limit is clamped to 1..20; an offset past the end is an empty last page.
    assert len(paged_items(
        [{"id": str(index)} for index in range(30)], limit=999, offset=0,
        id_key="id", field_limits={}, failure="x",
    )["items"]) == 20
    assert len(paged_items(
        [{"id": "a"}, {"id": "b"}], limit=0, offset=0, id_key="id",
        field_limits={}, failure="x",
    )["items"]) == 1
    past = paged_items(
        [{"id": "a"}], limit=5, offset=4, id_key="id", field_limits={}, failure="x"
    )
    assert past["items"] == [] and past["next_offset"] is None and past["total"] == 1

    # One row that cannot be packed at all is an error, not an empty page.
    with pytest.raises(AgentToolError) as failure:
        paged_items(
            [{"notebook_id": "n" * 40_000}], limit=5, offset=0,
            id_key="notebook_id", field_limits={}, failure="暂时无法完整返回",
        )
    assert failure.value.code == "internal"
    assert failure.value.message == "暂时无法完整返回"
