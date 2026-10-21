"""``get_conversation`` / ``list_conversations`` page building, as pure functions.

``turn_page`` turns one normalised answer view into one resumable page,
``conversation_row`` is the one shape a conversation is shown in, and
``list_page`` pages the list. The global branch's error translation runs here
against a fake service. The end-to-end behaviour (authorization, visibility,
the call ledger) is in ``tests/test_mcp_conversations.py`` and its PostgreSQL
twin.
"""
from __future__ import annotations

import pytest

from app.api.mcp_tools import global_ask as global_tools
from app.api.mcp_tools._shared import AgentToolError, _serialized_size
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
    # its ref -- the page cannot be built at all.
    unfit = _citation(1, element_id="el-" + "y" * 4_600)
    assert len(unfit["ref"]) > 6_000 and len(unfit["element_id"]) < 6_000
    with pytest.raises(AgentToolError) as refused:
        _page(_view("回答。", [unfit]))
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

