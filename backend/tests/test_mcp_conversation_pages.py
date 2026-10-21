"""``get_conversation`` / ``list_conversations`` page building, as pure functions.

``turn_page`` turns one normalised answer view into one resumable page,
``conversation_row`` is the one shape a conversation is shown in, and
``list_page`` pages the list. The global branch's error translation runs here
against a fake service. The end-to-end behaviour (authorization, visibility,
the call ledger) is in ``tests/test_mcp_conversations.py`` and its PostgreSQL
twin.
"""
from __future__ import annotations

import json

import pytest

from app.api.mcp_tools import global_ask as global_tools
from app.api.mcp_tools._shared import AgentToolError, _budget_response, _serialized_size
from app.api.mcp_tools.ask_pages import linked_report_view
from app.api.mcp_tools.conversations import (
    _CONVERSATION_NOT_FOUND,
    _global_row,
    _global_turn,
    conversation_row,
    empty_turn_page,
    list_page,
    newest_first,
    turn_page,
)
from app.api.mcp_tools.refs import decode_ref, element_ref
from app.models.ask import LinkedReportReceipt, LinkedReportSection
from app.models.global_ask import GlobalConversationSummary, GlobalNotebookScope
from app.services.global_ask import AgentGlobalConversation, GlobalAskError

# §2.3: the 17 keys of a turn page, plus the budget's ``truncation``.
PAGE_KEYS = {
    "conversation", "turn_offset", "next_turn_offset", "question", "asked_at",
    "submitted_via", "status", "mode", "answer", "next_answer_offset",
    "citations", "next_citation_offset", "total_citations",
    "completeness_notice", "error", "scope", "content_is_untrusted_evidence",
    "truncation",
}
ROW_KEYS = {
    "conversation_id", "kind", "title", "notebook_ids", "notebook_count",
    "turn_count", "updated_at", "submitted_via",
}
NOTEBOOK = "nb-a"


def _body(length: int) -> str:
    """Chinese text with distinct, order-sensitive characters."""
    return "".join(chr(0x4E00 + (index * 7919) % 4000) for index in range(length))


def _citation(index: int, *, element_id: str = "", quoted_span: str = "") -> dict:
    element_id = element_id or f"el-{index}"
    return {
        "label": f"来源{index}", "source_id": f"src-{index}", "element_id": element_id,
        "location_label": f"第{index}页",
        "quoted_span": quoted_span or "这是被引用的原文摘录。" * 3,
        "source_file_name": f"paper-{index}.pdf", "tier": "personal",
        "content_is_untrusted_evidence": True,
        "ref": element_ref(NOTEBOOK, f"src-{index}", element_id),
    }


def _view(answer: str = "回答。", citations=(), **overrides) -> dict:
    view = {
        "job_id": "", "conversation_id": "conv-1", "status": "answered",
        "mode": "reasoning", "answer": answer, "citations": list(citations),
        "completeness_notice": "", "error": "",
        "scope": {"kind": "notebook", "notebook_ids": [NOTEBOOK]},
    }
    view.update(overrides)
    return view


def _row(**overrides) -> dict:
    fields = {
        "conversation_id": "conv-1", "kind": "notebook", "title": "带宽",
        "notebook_ids": [NOTEBOOK], "turn_count": 3,
        "updated_at": "2026-10-21T08:00:00+00:00", "submitted_via": "web",
    }
    fields.update(overrides)
    return conversation_row(**fields)


def _page(view, *, row=None, question="带宽够吗", turn_offset=0, turn_count=3,
          answer_offset=0, citation_offset=0):
    return turn_page(
        row or _row(), view, question=question, asked_at="2026-10-21T08:00:00+00:00",
        submitted_via="web", turn_offset=turn_offset, turn_count=turn_count,
        answer_offset=answer_offset, citation_offset=citation_offset,
    )


def test_turn_page_fits_the_budget_and_cursors_resume_exactly() -> None:
    answer = _body(5_000)
    citations = [_citation(index) for index in range(45)]
    view = _view(answer, citations)
    row = _row()

    text, keys = "", []
    answer_offset, citation_offset, pages = 0, 0, 0
    while answer_offset is not None or citation_offset is not None:
        page = _page(
            view, row=row,
            answer_offset=len(answer) if answer_offset is None else answer_offset,
            citation_offset=45 if citation_offset is None else citation_offset,
        )
        pages += 1
        assert _serialized_size(page) <= 12_000
        assert set(page) == PAGE_KEYS
        assert page["conversation"] == row and page["question"] == "带宽够吗"
        assert page["total_citations"] == 45 and page["next_turn_offset"] == 1
        text += page["answer"]
        keys.extend(item["element_id"] for item in page["citations"])
        answer_offset = page["next_answer_offset"]
        citation_offset = page["next_citation_offset"]
    assert text == answer
    assert keys == [f"el-{index}" for index in range(45)]
    assert pages > 3, "the page budget had to cut, so the shrink path ran"

    # The last turn has no next one; a cursor past the end is an empty page.
    last = _page(view, turn_offset=2, answer_offset=5_000, citation_offset=45)
    assert last["next_turn_offset"] is None
    assert last["answer"] == "" and last["citations"] == []
    assert last["next_answer_offset"] is None and last["next_citation_offset"] is None


def test_turn_page_keeps_every_ref_whole() -> None:
    # Long element ids make long refs, and long excerpts pressure the budget:
    # a delivered citation keeps its ref exactly, or the page shrinks.
    citations = [
        _citation(index, element_id=f"el-{index}-" + "x" * 400,
                  quoted_span="摘录" * 150)
        for index in range(30)
    ]
    view = _view(_body(1_000), citations)
    offset, seen = 0, 0
    while offset is not None:
        page = _page(view, citation_offset=offset)
        for delivered in page["citations"]:
            original = citations[seen]
            assert delivered["ref"] == original["ref"]
            kind, ids = decode_ref(delivered["ref"])
            assert kind == "el" and ids["e"] == original["element_id"]
            seen += 1
        offset = page["next_citation_offset"]
    assert seen == 30

    # A ref too long for the output budget is dropped whole by it while the
    # citation's ids still fit: such a citation is never delivered without
    # its ref. The text still reads and the citation cursor stays put; once no
    # text is left the page cannot be built at all -- the one way left to an
    # ``internal``, reachable only with identifiers longer than the budget.
    unfit = _citation(1, element_id="el-" + "y" * 4_600)
    assert len(unfit["ref"]) > 6_000 and len(unfit["element_id"]) < 6_000
    page = _page(_view("回答。", [unfit]))
    assert page["answer"] == "回答。" and page["citations"] == []
    assert page["next_citation_offset"] == 0 and page["next_answer_offset"] is None
    with pytest.raises(AgentToolError) as refused:
        _page(_view("回答。", [unfit]), answer_offset=3)
    assert refused.value.code == "internal"


def test_turn_page_never_clips_the_conversation_row() -> None:
    # Citations whose labels crowd the budget: the budget would shorten the
    # longest strings, the conversation title among them. The page shrinks
    # instead, and every page repeats the row exactly.
    row = _row(title="会话标题" * 50)
    citations = [
        {**_citation(index), "label": "标" * 300, "location_label": "位" * 300}
        for index in range(20)
    ]
    view = _view("短回答。", citations)
    offset, seen = 0, 0
    while offset is not None:
        page = _page(view, row=row, citation_offset=offset)
        assert page["conversation"] == row
        seen += len(page["citations"])
        offset = page["next_citation_offset"]
    assert seen == 20


def test_long_question_is_clipped_and_reported() -> None:
    question = _body(4_000)
    page = _page(_view(), question=question)
    assert len(page["question"]) == 2_000 and page["question"].endswith("…")
    assert page["question"][:1_999] == question[:1_999]
    assert page["truncation"]["truncated"] is True
    assert page["truncation"]["omitted_characters"] >= 4_000 - 1_999

    short = _page(_view(), question="短问题")
    assert short["question"] == "短问题"
    assert short["truncation"]["truncated"] is False


def _read_turn(view, *, row, question):
    """Every page of one turn, following both cursors to the end (a finished
    cursor is parked at its total, as a caller reading the other one does)."""
    answer_total = len(view["answer"])
    citation_total = len(view["citations"])
    pages, answer_offset, citation_offset = [], 0, 0
    while answer_offset is not None or citation_offset is not None:
        page = _page(
            view, row=row, question=question,
            answer_offset=answer_total if answer_offset is None else answer_offset,
            citation_offset=citation_total if citation_offset is None else citation_offset,
        )
        pages.append(page)
        assert _serialized_size(page) <= 12_000 and set(page) == PAGE_KEYS
        assert page["conversation"] == row
        answer_offset = page["next_answer_offset"]
        citation_offset = page["next_citation_offset"]
        assert len(pages) < 200, "a cursor stopped moving"
    return pages


def _full_citation(index: int) -> dict:
    """A citation whose display fields are all at their caps."""
    source_id, element_id = f"src-{index:03d}-" + "s" * 40, f"el-{index:03d}-" + "e" * 40
    return {
        "label": "标" * 300, "source_title": "题" * 300, "location_label": "位" * 300,
        "quoted_span": "摘" * 200, "source_file_name": "文" * 300, "tier": "personal",
        "source_id": source_id, "element_id": element_id,
        "notebook_id": f"nb-{index % 30:02d}-" + "n" * 30,
        "content_is_untrusted_evidence": True,
        "ref": element_ref(NOTEBOOK, source_id, element_id),
    }


def test_continuation_pages_carry_a_short_question() -> None:
    question = _body(2_000)
    view = _view(_body(6_000), [_citation(index) for index in range(20)])
    pages = _read_turn(view, row=_row(), question=question)
    first, rest = pages[0], pages[1:]
    # The first page carries the whole question; no cut is reported for it.
    assert first["question"] == question
    assert first["truncation"]["omitted_characters"] == 0
    assert rest, "the turn took more than one page"
    for page in rest:
        assert len(page) == 18
        assert len(page["question"]) <= 200 and page["question"].endswith("…")
        assert page["question"][:199] == question[:199]
        assert page["truncation"]["truncated"] is True
        assert page["truncation"]["omitted_characters"] >= 2_000 - 199
    # With the room the short question leaves, the turn reads in few calls.
    assert len(pages) <= 6, len(pages)
    assert "".join(page["answer"] for page in pages) == view["answer"]


@pytest.mark.parametrize("question_chars", [1_500, 2_000])
def test_every_turn_reads_to_the_end(question_chars) -> None:
    # The worst page a real turn makes: a long question, a long answer,
    # citations whose fields are all at their caps, a global scope over 30
    # libraries and a row naming its libraries. Every page fits, the cursors
    # always move, and nothing is skipped -- never ``internal``.
    question = _body(question_chars)
    libraries = [f"nb-{index:02d}-" + "n" * 30 for index in range(30)]
    row = _row(conversation_id="gconv-1", kind="global", title="题" * 200,
               notebook_ids=libraries)
    view = _view(
        _body(6_000), [_full_citation(index) for index in range(25)],
        scope={"kind": "global", "mode": "include", "notebook_ids": libraries},
    )
    pages = _read_turn(view, row=row, question=question)
    assert pages[0]["question"] == question
    assert "".join(page["answer"] for page in pages) == view["answer"]
    delivered = [item["element_id"] for page in pages for item in page["citations"]]
    assert delivered == [item["element_id"] for item in view["citations"]]
    assert all(len(page["question"]) <= 200 for page in pages[1:])


def test_question_is_cut_on_the_first_page_only_when_nothing_else_fits() -> None:
    # A row whose library ids alone take half the budget leaves no room for a
    # 2,000-character question beside even one character of text: the first
    # page cuts the question the way a continuation page does, and says so.
    row = _row(notebook_ids=[f"nb-{index:02d}-" + "n" * 290 for index in range(20)])
    question = _body(2_000)
    view = _view(_body(3_000))
    pages = _read_turn(view, row=row, question=question)
    first = pages[0]
    assert first["question"].endswith("…") and len(first["question"]) < 2_000
    assert first["question"][:-1] == question[:len(first["question"]) - 1]
    assert first["truncation"]["omitted_characters"] >= 2_000 - len(first["question"])
    assert first["answer"], "the page still moved the text cursor"
    assert "".join(page["answer"] for page in pages) == view["answer"]


def test_turn_page_has_room_for_two_more_keys() -> None:
    page = _page(_view(citations=[_citation(1)]))
    # 17 keys + ``truncation``: two below the 20-key response cap, kept free
    # for per-turn fields a later feature adds.
    assert len(page) <= 18
    assert set(page) == PAGE_KEYS
    empty = empty_turn_page(_row(turn_count=0))
    assert set(empty) == {
        "conversation", "turn_offset", "next_turn_offset", "status",
        "content_is_untrusted_evidence", "truncation",
    }
    assert empty["status"] == "empty" and empty["next_turn_offset"] is None
    assert empty["turn_offset"] == 0 and empty["conversation"]["turn_count"] == 0


def test_conversation_row_is_one_shape_for_both_kinds() -> None:
    notebook = _row()
    assert set(notebook) == ROW_KEYS
    assert notebook["notebook_ids"] == [NOTEBOOK] and notebook["notebook_count"] == 1

    libraries = tuple(sorted(f"nb-{index:02d}" for index in range(25)))
    summary = GlobalConversationSummary(
        id="gconv-1", title="长" * 300, created_at="2026-10-21T08:00:00+00:00",
        updated_at="2026-10-21T09:00:00+00:00",
        # The latest turn's scope names a library that was deleted since: the
        # row never repeats it -- it names what the visibility rule judged.
        notebook_scope=GlobalNotebookScope(
            mode="include", notebook_ids=["nb-deleted", "nb-00"]
        ),
        submitted_via="mcp",
    )
    row = _global_row(AgentGlobalConversation(
        summary=summary, notebook_ids=tuple(reversed(libraries)), turn_count=4,
    ))
    assert set(row) == ROW_KEYS
    assert row["kind"] == "global" and row["conversation_id"] == "gconv-1"
    assert row["notebook_ids"] == list(libraries[:20])
    assert row["notebook_count"] == 25 and "nb-deleted" not in row["notebook_ids"]
    assert row["turn_count"] == 4 and row["submitted_via"] == "mcp"
    assert len(row["title"]) == 200 and row["title"].endswith("…")
    # The same row, budgeted on a page, survives whole.
    page = _page(_view(), row=row, turn_count=4)
    assert page["conversation"] == row


REPORT = {"report_id": "rep-1a2b3c4d", "title": "光伏组件封装工艺综述"}


def test_conversation_row_names_the_followed_report_and_nothing_else() -> None:
    row = _row(linked_report={
        **REPORT, "status": "read", "sections_read": ["背景"],
        "memory_used": ["mem-1"],
    })
    assert set(row) == ROW_KEYS | {"linked_report"}
    assert row["linked_report"] == REPORT
    # Without a report the row is the eight keys it always was: the key is
    # left out, never ``None`` or an empty mapping.
    assert set(_row()) == ROW_KEYS
    for none in (None, {}, {"report_id": ""}, {"title": "没有编号的标题"}):
        assert set(_row(linked_report=none)) == ROW_KEYS, none
    # An empty title is still a report: the id is what the caller opens.
    assert _row(linked_report={"report_id": "rep-9"})["linked_report"] == {
        "report_id": "rep-9", "title": "",
    }
    # The cut is the page's, so the row itself hands the title over whole.
    long = _body(500)
    assert _row(linked_report={**REPORT, "title": long})["linked_report"]["title"] == long


def test_a_page_repeats_the_followed_report_and_keeps_its_key_budget() -> None:
    row = _row(linked_report=REPORT)
    page = _page(_view(), row=row)
    assert page["conversation"] == row
    # The report rides inside ``conversation`` and ``scope``: no new key.
    assert set(page) == PAGE_KEYS and len(page) == 18
    assert page["truncation"]["omitted_characters"] == 0
    empty = empty_turn_page(_row(turn_count=0, linked_report=REPORT))
    assert empty["conversation"]["linked_report"] == REPORT


@pytest.mark.parametrize("chars,cut", [(199, 0), (200, 0), (201, 2), (500, 301)])
def test_a_followed_reports_title_is_cut_to_the_title_cap_and_counted(chars, cut) -> None:
    title = _body(chars)
    row = _row(linked_report={**REPORT, "title": title})
    page = _page(_view(), row=row)
    shown = page["conversation"]["linked_report"]["title"]
    assert shown == (title if not cut else title[:199] + "…")
    assert len(shown) == min(chars, 200)
    assert page["truncation"]["omitted_characters"] == cut
    assert page["conversation"]["linked_report"]["report_id"] == REPORT["report_id"]
    # The empty page cuts it by the same limit and counts it the same way.
    empty = empty_turn_page(row)
    assert empty["conversation"]["linked_report"]["title"] == shown
    assert empty["truncation"]["omitted_characters"] == cut


def test_every_page_of_a_turn_repeats_the_cut_row_and_counts_it() -> None:
    title = _body(500)
    row = _row(linked_report={**REPORT, "title": title})
    citations = [_citation(index) for index in range(30)]
    view = _view(_body(5_000), citations)
    answer_offset, citation_offset, pages = 0, 0, 0
    while answer_offset is not None or citation_offset is not None:
        page = _page(
            view, row=row,
            answer_offset=5_000 if answer_offset is None else answer_offset,
            citation_offset=30 if citation_offset is None else citation_offset,
        )
        pages += 1
        assert page["conversation"]["linked_report"] == {
            "report_id": REPORT["report_id"], "title": title[:199] + "…",
        }
        assert page["truncation"]["omitted_characters"] >= 301
        assert _serialized_size(page) <= 12_000 and set(page) == PAGE_KEYS
        answer_offset = page["next_answer_offset"]
        citation_offset = page["next_citation_offset"]
    assert pages >= 3, "a 5,000-character answer is at least three pages"


def test_a_full_page_keeps_the_receipt_whole() -> None:
    """The longest answer page (full text, twenty citations) beside the
    receipt of a read report: the receipt is not what the budget gives up."""
    receipt = LinkedReportReceipt(
        report_id=REPORT["report_id"], title=REPORT["title"], status="read",
        sections=[
            LinkedReportSection(index=index, title=f"第{index}节 " + "标" * 30, read=index % 2 == 1)
            for index in range(1, 7)
        ],
        summary_truncated=True, evidence_restored=7, evidence_gone=2,
        evidence_over_budget=1, memory_used=["mem-1"],
    )
    scope = {"kind": "notebook", "notebook_ids": [NOTEBOOK],
             "linked_report": linked_report_view(receipt)}
    view = _view(_body(2_000), [_full_citation(index) for index in range(20)], scope=scope)
    page = _page(view, row=_row(linked_report=REPORT))
    assert page["scope"] == scope
    assert page["scope"]["linked_report"]["sections_read"] == [
        f"第{index}节 " + "标" * 30 for index in (1, 3, 5)
    ]
    assert "memory_used" not in page["scope"]["linked_report"]
    assert "mem-1" not in json.dumps(page, ensure_ascii=False)


#: ``rep-`` plus 32 hex, the form a generated id has: longer than the 32
#: characters the budget's ordinary pass shrinks a string to.
GENERATED_REPORT_ID = "rep-" + "0123456789abcdef" * 2


def _receipt_scope(report_id: str = GENERATED_REPORT_ID) -> dict:
    receipt = LinkedReportReceipt(
        report_id=report_id, title=REPORT["title"], status="read",
        sections=[
            LinkedReportSection(index=index, title=f"第{index}节 " + "标" * 30, read=True)
            for index in range(1, 7)
        ],
        evidence_restored=7, evidence_gone=2, evidence_over_budget=1,
    )
    return {"kind": "notebook", "notebook_ids": [NOTEBOOK],
            "linked_report": linked_report_view(receipt)}


def test_the_report_id_of_a_turns_receipt_is_never_cut() -> None:
    """A short conversation id, the report on an EARLIER turn (so the row names
    none), and pages crowded by full-size citations: the budget shrinks the
    longest strings first, and a 36-character report id is not the longest --
    until the others are down to its length. It is an identifier: kept whole,
    and a page that did not keep it is refused, so the caller is never handed a
    handle that opens nothing."""
    scope = _receipt_scope()
    row = _row(conversation_id="conv-1")
    assert "linked_report" not in row
    citations = [_full_citation(index) for index in range(40)]
    view = _view(_body(6_000), citations, scope=scope)
    answer_offset, citation_offset, pages = 0, 0, 0
    while answer_offset is not None or citation_offset is not None:
        page = _page(
            view, row=row,
            answer_offset=6_000 if answer_offset is None else answer_offset,
            citation_offset=40 if citation_offset is None else citation_offset,
        )
        pages += 1
        assert page["scope"]["linked_report"]["report_id"] == GENERATED_REPORT_ID, pages
        assert _serialized_size(page) <= 12_000 and set(page) == PAGE_KEYS
        answer_offset = page["next_answer_offset"]
        citation_offset = page["next_citation_offset"]
    assert pages >= 4


def test_the_budget_sheds_everything_else_before_a_report_id() -> None:
    """The ordinary pass shrinks the longest strings first (down to 32
    characters) and then drops list items; a 36-character report id is an
    identifier, so it is neither shrunk by the first nor reached by the second."""
    packed = _budget_response({
        "scope": {"linked_report": {"report_id": GENERATED_REPORT_ID}},
        "rows": [{"label": f"标签{index:03d}" + "标" * 40} for index in range(400)],
    })
    assert packed["scope"]["linked_report"]["report_id"] == GENERATED_REPORT_ID
    assert packed["truncation"]["truncated"] is True, "the pressure was real"


def test_a_turn_page_never_carries_a_cut_report_id() -> None:
    """Only a report id longer than the whole response budget -- not one a
    report has -- is shortened, as the budget's last resort; the page that
    carries it is then refused at every size, never delivered with a handle
    that opens nothing."""
    scope = _receipt_scope("rep-" + "x" * 13_000)
    with pytest.raises(AgentToolError) as refused:
        _page(_view("回答。", scope=scope))
    assert refused.value.code == "internal"


def test_list_page_cuts_a_followed_reports_title_and_counts_it() -> None:
    long = _body(500)
    rows = [
        _row(conversation_id=f"conv-{index:02d}",
             linked_report={"report_id": f"rep-{index:02d}", "title": long})
        for index in range(5)
    ] + [_row(conversation_id="conv-plain")]
    page = list_page(rows, limit=20, offset=0)
    assert [item["conversation_id"] for item in page["items"]] == [
        row["conversation_id"] for row in rows
    ]
    for item, row in zip(page["items"], rows):
        if "linked_report" in row:
            assert item["linked_report"] == {
                "report_id": row["linked_report"]["report_id"],
                "title": long[:199] + "…",
            }
        else:
            assert set(item) == ROW_KEYS
    assert page["truncation"]["omitted_characters"] == 5 * 301


def test_list_page_keeps_long_report_ids_whole_and_sheds_titles_instead() -> None:
    # Report ids up to 200 characters are accepted by ``get_report``. Twenty rows
    # whose ids and titles are that long: an id is an identifier, so the budget
    # shortens the titles (and only then the rows) and never a report id.
    rows = [
        _row(conversation_id=f"conv-{index:02d}", title="标题" * 5,
             linked_report={
                 "report_id": f"rep-{index:02d}-" + "r" * 150,
                 "title": _body(500),
             })
        for index in range(20)
    ]
    seen, offset = [], 0
    while offset is not None:
        page = list_page(rows, limit=20, offset=offset)
        assert page["items"] and page["total"] == 20
        for item in page["items"]:
            original = rows[len(seen)]
            assert item["conversation_id"] == original["conversation_id"]
            assert item["linked_report"]["report_id"] == original["linked_report"]["report_id"]
            title = item["linked_report"]["title"]
            assert title.endswith("…") and len(title) <= 200
            assert original["linked_report"]["title"].startswith(title[:-1])
            seen.append(item["conversation_id"])
        offset = page["next_offset"]
    assert seen == [row["conversation_id"] for row in rows]


def test_list_page_never_delivers_a_row_with_a_cut_report_id() -> None:
    # Only an id longer than the whole response budget -- not one a report has --
    # is shortened by the budget, as the last resort. A row naming such an id
    # must not count as delivered (the rows before it still page, and the cursor
    # stops at it); read alone it is refused rather than handed over cut.
    rows = [_row(conversation_id=f"conv-{index}",
                 linked_report={"report_id": f"rep-{index}", "title": "短"})
            for index in range(3)]
    rows.append(_row(conversation_id="conv-big",
                     linked_report={"report_id": "rep-" + "x" * 13_000, "title": "短"}))
    page = list_page(rows, limit=20, offset=0)
    assert [item["conversation_id"] for item in page["items"]] == [
        "conv-0", "conv-1", "conv-2",
    ]
    assert [item["linked_report"]["report_id"] for item in page["items"]] == [
        "rep-0", "rep-1", "rep-2",
    ]
    assert page["next_offset"] == 3
    with pytest.raises(AgentToolError) as refused:
        list_page(rows, limit=20, offset=3)
    assert refused.value.code == "internal"


def test_list_order_parses_mixed_timestamp_spellings() -> None:
    rows = [
        {"conversation_id": "conv-a", "updated_at": "2026-10-21T08:00:00"},
        {"conversation_id": "gconv-b", "updated_at": "2026-10-21T15:30:00+08:00"},
        {"conversation_id": "conv-c", "updated_at": "2026-10-21T07:45:00Z"},
        {"conversation_id": "conv-d", "updated_at": "2026-10-21T08:00:00+00:00"},
        {"conversation_id": "gconv-e", "updated_at": "not a time"},
    ]
    # 15:30+08:00 is 07:30Z: by text it would sort first. conv-a (bare, read as
    # UTC) and conv-d are the same instant: the larger id wins the tie.
    assert [row["conversation_id"] for row in newest_first(rows)] == [
        "conv-d", "conv-a", "conv-c", "gconv-b", "gconv-e",
    ]


def test_list_page_never_cuts_a_rows_notebooks() -> None:
    # Twenty rows naming twenty long notebook ids each: the budget, having
    # shortened the titles as far as it does, cuts a list. A row whose
    # notebooks were cut must not count as delivered.
    rows = [
        _row(conversation_id=f"gconv-{index:02d}", kind="global",
             title="很长的标题" * 40,
             notebook_ids=[f"nb-{index:02d}-{item:02d}-" + "x" * 20 for item in range(20)])
        for index in range(20)
    ]
    seen, offset, sizes = [], 0, []
    while offset is not None:
        page = list_page(rows, limit=20, offset=offset)
        assert page["items"] and page["total"] == 20
        for item in page["items"]:
            original = rows[len(seen)]
            assert item["conversation_id"] == original["conversation_id"]
            assert item["notebook_ids"] == original["notebook_ids"]
            assert item["notebook_count"] == 20
            seen.append(item["conversation_id"])
        sizes.append(len(page["items"]))
        offset = page["next_offset"]
    assert seen == [row["conversation_id"] for row in rows]
    assert min(sizes) < 20, "the budget cut a page, so the cut path ran"


# ------------------------------------------------- the global branch's errors


class _FakeService:
    def __init__(self, outcome):
        self.outcome = outcome

    def agent_conversation_turn(self, conversation_id, **_kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _Principal:
    owner_id = "owner-a"
    notebook_ids = [NOTEBOOK]


def _read_global(monkeypatch, outcome, turn_offset=0):
    monkeypatch.setattr(global_tools, "global_ask_service", lambda: _FakeService(outcome))
    return _global_turn(
        _Principal(), "gconv-1", turn_offset=turn_offset, answer_offset=0,
        citation_offset=0,
    )


def _conversation(turn_count: int) -> AgentGlobalConversation:
    summary = GlobalConversationSummary(
        id="gconv-1", title="全局", created_at="2026-10-21T08:00:00+00:00",
        updated_at="2026-10-21T08:00:00+00:00",
        notebook_scope=GlobalNotebookScope(), submitted_via="web",
    )
    return AgentGlobalConversation(
        summary=summary, notebook_ids=(NOTEBOOK,), turn_count=turn_count,
    )


def test_global_refusals_all_read_as_one_not_found(monkeypatch) -> None:
    # The service words "missing" and "a library is no longer allowed"
    # differently; the tool must not, or the wording would tell a caller that
    # a conversation exists.
    refusals = [
        GlobalAskError(404, "对话不存在，请刷新列表。"),
        GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。"),
        GlobalAskError(403, "访问权限已变化，请重新连接后重试。"),
    ]
    errors = set()
    for refusal in refusals:
        with pytest.raises(AgentToolError) as refused:
            _read_global(monkeypatch, refusal)
        errors.add((refused.value.code, refused.value.message))
    assert errors == {("not_found", _CONVERSATION_NOT_FOUND)}


def test_global_turn_that_vanished_after_the_judgement_is_not_found(monkeypatch) -> None:
    # Judged visible with 2 finished turns, then turn 1 went away before it
    # was read: the service hands back no job. Never paged as an empty turn.
    with pytest.raises(AgentToolError) as refused:
        _read_global(monkeypatch, (_conversation(2), None), turn_offset=1)
    assert (refused.value.code, refused.value.message) == (
        "not_found", _CONVERSATION_NOT_FOUND,
    )
    # Past the last turn is the caller's offset, not a missing conversation.
    with pytest.raises(AgentToolError) as refused:
        _read_global(monkeypatch, (_conversation(2), None), turn_offset=2)
    assert refused.value.code == "invalid_argument"
    assert "共 2 轮" in refused.value.message
    # No finished turn yet: the empty page.
    page = _read_global(monkeypatch, (_conversation(0), None))
    assert page["status"] == "empty" and page["conversation"]["turn_count"] == 0


def test_global_errors_other_than_refusals_keep_their_meaning(monkeypatch) -> None:
    with pytest.raises(AgentToolError) as busy:
        _read_global(monkeypatch, GlobalAskError(503, "全局问答暂不可用，请稍后重试。"))
    assert busy.value.code == "unavailable"
    with pytest.raises(AgentToolError) as internal:
        _read_global(monkeypatch, RuntimeError("SELECT * FROM secret_table"))
    assert internal.value.code == "internal"
    assert "secret_table" not in internal.value.message

