"""Shared behaviour of the report a conversation names (``ask_jobs.report_id``
and the ``linked_report`` receipt in ``answers.payload``), run by
``test_conversation_linked_report_store.py`` and its PostgreSQL twin
``tests/postgres/test_conversation_linked_report_store_pg.py``.

The two places a conversation names a report are read by two different rules
(see ``app/repositories/conversation_linked_report.py``):

* the in-flight turn's title is the report's CURRENT question, empty once the
  report is gone, and only a report of the SAME notebook counts;
* the summary and the detail take the LAST turn's receipt, whose title is a
  snapshot -- and the two must always agree.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from app.models.ask import (
    AskRequest,
    LinkedReportReceipt,
    LinkedReportRef,
)
from app.models.notebooks import NotebookCreate

#: ``save_turn(notebook_id, conversation_id | None, question, receipt | None)``
#: saves one answered turn (creating the conversation when the id is None) and
#: returns the conversation id. Supplied per backend by the test module.
SaveTurn = Callable[[str, Optional[str], str, Optional[LinkedReportReceipt]], str]


def _receipt(report_id: str, title: str, *, status: str = "read") -> LinkedReportReceipt:
    if status == "read":
        return LinkedReportReceipt(report_id=report_id, title=title, status="read")
    return LinkedReportReceipt(
        report_id=report_id, title=title, status="skipped",
        reason_code="scope_sources", reason="本轮不读取这份报告：它引用的 1 份资料不在当前检索范围内",
    )


def _summary(repo, notebook_id: str, conversation_id: str):
    return next(
        item for item in repo.list_conversations(notebook_id)
        if item.id == conversation_id
    )


def assert_job_row_records_the_report(repo, stored_report_id: Callable[[str], str]) -> None:
    """Every begin path writes ``ask_jobs.report_id`` and the origin / detail
    reads give it back ('' = no report). A keyed retry attaches to the first
    row instead of overwriting it."""
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="report-id")).id

    plain, _conv = store.begin_durable_job(
        nb, AskRequest(question="Q?", mode="chunk", report_id="rep-plain"),
        "chunk", uid, submitted_via="web")
    keyed, _conv, attached = store.begin_or_attach_durable_job(
        nb, AskRequest(question="Q?", mode="chunk", report_id="rep-keyed",
                       client_request_id="report-key-1"),
        "chunk", uid, submitted_via="web")
    keyless, _conv, _ = store.begin_or_attach_durable_job(
        nb, AskRequest(question="Q?", mode="chunk", report_id="rep-keyless"),
        "chunk", uid, submitted_via="web")
    assert attached is False
    for job_id, expected in ((plain, "rep-plain"), (keyed, "rep-keyed"),
                             (keyless, "rep-keyless")):
        assert store.ask_job_origin(job_id)["report_id"] == expected
        assert repo.ask_job_detail(job_id)["report_id"] == expected
        assert stored_report_id(job_id) == expected

    retry, _conv, attached = store.begin_or_attach_durable_job(
        nb, AskRequest(question="Q?", mode="chunk", report_id="rep-other",
                       client_request_id="report-key-1"),
        "chunk", uid, submitted_via="web")
    assert attached is True and retry == keyed
    assert store.ask_job_origin(keyed)["report_id"] == "rep-keyed"
    assert stored_report_id(keyed) == "rep-keyed"

    # A request that names no report -- absent, or a blank string the request
    # model normalises away -- is stored as ''.
    for request in (AskRequest(question="Q?", mode="chunk"),
                    AskRequest(question="Q?", mode="chunk", report_id="   ")):
        job_id, _conv = store.begin_durable_job(nb, request, "chunk", uid)
        assert store.ask_job_origin(job_id)["report_id"] == ""
        assert repo.ask_job_detail(job_id)["report_id"] == ""
        assert stored_report_id(job_id) == ""


def assert_in_flight_turn_names_the_report_by_its_current_title(repo) -> None:
    """``ActiveAskJob.linked_report``: the job row's id plus the report's
    CURRENT question; a deleted report keeps the id and loses the title; a
    report of another notebook is not this notebook's report."""
    nb = repo.create_notebook(NotebookCreate(name="in-flight")).id
    other = repo.create_notebook(NotebookCreate(name="elsewhere")).id
    report_id = repo.create_report(nb, "Why does the gain drop at 3 GHz?")

    def begin(report: Optional[str]) -> str:
        payload = AskRequest(question="Follow-up?", mode="chunk", report_id=report)
        _job, conversation_id = repo.begin_ask_job(
            nb, payload, "chunk", threading.Event())
        return conversation_id

    with_report = begin(report_id)
    detail = repo.get_conversation(with_report)
    assert detail.active_job is not None
    assert detail.active_job.linked_report == LinkedReportRef(
        report_id=report_id, title="Why does the gain drop at 3 GHz?")
    assert detail.active_job.model_dump(mode="json")["linked_report"] == {
        "report_id": report_id, "title": "Why does the gain drop at 3 GHz?"}
    # No turn has been answered yet, so there is no receipt to summarise.
    assert detail.linked_report is None
    assert "linked_report" not in detail.model_dump(mode="json")

    without_report = begin(None)
    plain = repo.get_conversation(without_report).active_job
    assert plain is not None and plain.linked_report is None
    assert "linked_report" not in plain.model_dump(mode="json")

    foreign = begin(repo.create_report(other, "A question of another notebook"))
    foreign_ref = repo.get_conversation(foreign).active_job.linked_report
    assert foreign_ref is not None and foreign_ref.title == ""

    # The title follows the report: a deleted one leaves only the id.
    repo.delete_report(nb, report_id)
    gone = repo.get_conversation(with_report).active_job.linked_report
    assert gone == LinkedReportRef(report_id=report_id, title="")


def assert_summary_and_detail_follow_the_last_turn(repo, save_turn: SaveTurn) -> None:
    """The summary and the detail both name the report of the conversation's
    LAST answer (a skipped turn still named its report), the title is the
    receipt's snapshot, and a conversation whose last turn attached nothing
    names nothing -- the sticky tag is the last turn's, not 'any turn's'."""
    nb = repo.create_notebook(NotebookCreate(name="last-turn")).id
    report_a = repo.create_report(nb, "Report A question")
    report_b = repo.create_report(nb, "Report B question")

    def both(conversation_id: str):
        summary = _summary(repo, nb, conversation_id).linked_report
        detail = repo.get_conversation(conversation_id).linked_report
        assert summary == detail, (summary, detail)
        return summary

    conversation = save_turn(nb, None, "q1", _receipt(report_a, "Report A question"))
    assert both(conversation) == LinkedReportRef(
        report_id=report_a, title="Report A question")

    conversation = save_turn(nb, conversation, "q2", None)
    assert both(conversation) is None
    assert "linked_report" not in _summary(repo, nb, conversation).model_dump(mode="json")
    assert "linked_report" not in repo.get_conversation(conversation).model_dump(mode="json")

    conversation = save_turn(
        nb, conversation, "q3", _receipt(report_b, "Report B question", status="skipped"))
    assert both(conversation) == LinkedReportRef(
        report_id=report_b, title="Report B question")
    # The per-turn receipts stay on the turns; only the last one is summarised.
    turns = repo.get_conversation(conversation).turns
    assert [t.response.linked_report.report_id if t.response.linked_report else None
            for t in turns] == [report_a, None, report_b]

    # The title is the receipt's snapshot: deleting the report changes nothing.
    repo.delete_report(nb, report_b)
    assert both(conversation) == LinkedReportRef(
        report_id=report_b, title="Report B question")

    # Another conversation of the same notebook is summarised on its own.
    other_conversation = save_turn(
        nb, None, "q-other", _receipt(report_a, "Report A question"))
    assert both(other_conversation).report_id == report_a
    assert both(conversation).report_id == report_b


def assert_unreadable_receipt_reads_as_no_report(
    repo,
    save_turn: SaveTurn,
    overwrite_payload: Callable[[str, object], None],
) -> None:
    """A receipt the projection cannot use never breaks the conversation list:
    an empty report id reads as 'no report' on the summary AND the detail, and
    a stored value that is not even an object reads as none on the summary."""
    nb = repo.create_notebook(NotebookCreate(name="unreadable")).id
    conversation = save_turn(nb, None, "q1", None)

    overwrite_payload(conversation, {
        "report_id": "", "title": "no id", "status": "read"})
    assert _summary(repo, nb, conversation).linked_report is None
    assert repo.get_conversation(conversation).linked_report is None

    for junk in ("oops", [], 7, {"title": "no id"}, {"report_id": 5, "title": "x"}):
        overwrite_payload(conversation, junk)
        assert _summary(repo, nb, conversation).linked_report is None, junk
