"""AskService's handling of the deep report a question attaches (plan
2026-10-21 T6): the report seat, the computation at the engine entry, the
pre-send check, the understanding step, the idempotent retry and the receipt
every such answer carries.

The verdict-and-restore scenarios run on a real SQLite repository here and on
PostgreSQL in ``tests/postgres/test_ask_report_followup_pg.py``
(``tests/ask_report_followup_cases.py``). The rest pins the service's own
rules on narrow doubles: what is read and when, what fails soft and what never
does, and the deterministic clarification gate standing down for a request
that names a report.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.cancellation import AskCancelled
from app.domain.report_followup import (
    REASON_ENGINE_UNSUPPORTED,
    REASON_NO_SYNTHESIS,
    REASON_READ_FAILED,
    REASON_SCOPE_SOURCES,
    ReportRestoreScopeError,
    RestoredReportEvidence,
)
from app.domain.retrieval import RetrievedChunk
from app.domain.retrieval_control import RetrievalControlError
from app.models.ask import AskRequest
from app.models.source_scope import SourceScope
from app.repositories.ports import AskRequestKeyReused
from app.services.ask_service import AskService
from app.services.prompts import ATTACHED_REPORT_REFERENCE_RULE, query_intent_prompt
from app.services.query_intent import followup_gate, plan_query_intent
from app.services.report_followup import ReportFollowupReaders
from tests import ask_report_followup_cases as cases
from tests.model_testkit import bind_chat_client
from tests.report_reference_restore_cases import as_request_user
from tests.test_ask_service_boundary import _minimal_ask_service, static_ceiling_readers


@pytest.fixture
def world(tmp_path, monkeypatch):
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'followup.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        yield cases.build_world(repo, postgres=False)
    finally:
        repo.close()


@pytest.mark.parametrize(
    "scenario", cases.SCENARIOS, ids=lambda scenario: scenario.__name__,
)
def test_ask_report_followup(world, scenario):
    scenario(world)


# ------------------------------------------------------------ narrow doubles
REPORT_ID = "rep-1"
REPORT_ROW = {
    "id": REPORT_ID, "notebook_id": "nb", "question": "报告题目",
    "status": "done", "created_by": "user",
    "sections": [{"title": "第一节"}],
    "content_md": "## 执行摘要\n摘要。\n\n## 第一节\n正文 [k1]。\n",
    "references": [
        {"key": "k1", "object_type": "chunk", "object_id": "chunk-1",
         "source_id": "source-1"},
    ],
}
RESTORED = RestoredReportEvidence(
    chunks=(RetrievedChunk(
        chunk_id="chunk-1", source_id="source-1", source_title="Source",
        section_path="§1", text="evidence", element_ids=[],
    ),),
    key_by_object={("chunk", "chunk-1"): ("k1",)},
)


def _readers(calls: list, *, row=REPORT_ROW, fail=None) -> ReportFollowupReaders:
    """Report readers that log each call by name and raise ``fail[name]``."""
    fail = fail or {}

    def logged(name, answer):
        def read(*args, **kwargs):
            calls.append(name)
            if name in fail:
                raise fail[name]
            return answer(*args, **kwargs)
        return read

    def read_report(notebook_id, report_id):
        if notebook_id != row["notebook_id"] or report_id != row["id"]:
            raise KeyError(report_id)
        return dict(row)

    return ReportFollowupReaders(
        read_report=logged("read_report", read_report),
        source_owners=logged("source_owners", lambda ids: {i: "nb" for i in ids}),
        object_owners=logged("object_owners", lambda ids: {i: "nb" for i in ids}),
        participant_notebook_ids=logged(
            "participant_notebook_ids", lambda nb, *, viewer_id: (nb,),
        ),
    )


def _service(calls: list, *, visible=("source-1",), fail=None, **overrides):
    """A minimal chunk service whose ceiling admits ``visible`` and whose
    restore is a logged stub; events land in ``service.events``."""
    overrides.setdefault("report_followup_readers", _readers(calls, fail=fail))
    service = _minimal_ask_service(
        ceiling_readers=static_ceiling_readers(visible=lambda _nb: tuple(visible)),
        **overrides,
    )
    service.events = []
    service.event_log = SimpleNamespace(emit=service.events.append)
    failure = (fail or {}).get("restore")

    def restore(notebook_id, actor_id, cited):
        calls.append("restore")
        service.restore_requests = (notebook_id, actor_id, list(cited))
        if failure is not None:
            raise failure
        return RESTORED

    service.candidates.restore_report_references = restore
    return service


def _ask(service, report_id=REPORT_ID, **payload):
    return service.ask(
        "nb", AskRequest(question="q", report_id=report_id, **payload),
        user_id="user",
    )


def _events(service, kind):
    return [event for event in service.events if event.get("kind") == kind]


def test_a_readable_report_is_judged_and_read_back_once_and_the_answer_says_so():
    """The engine entry reads the report row, the one owner map it needs and
    -- the verdict allowing it -- restores the cited items once, through the
    candidate port, as the asker. An engine that wrote no receipt of its own
    (the answer model is not configured here) still answers with one:
    ``skipped`` / ``no_synthesis``, and one content-free event mirrors it."""
    calls: list = []
    service = _service(calls)

    response = _ask(service)

    assert calls == ["read_report", "source_owners", "restore"]
    notebook_id, actor_id, cited = service.restore_requests
    assert (notebook_id, actor_id) == ("nb", "user")
    assert [(ref.key, ref.object_id, ref.notebook_id) for ref in cited] == [
        ("k1", "chunk-1", "nb"),
    ]
    assert response.conclusion  # the answer stands
    receipt = response.linked_report
    assert (receipt.report_id, receipt.title, receipt.status, receipt.reason_code) == (
        REPORT_ID, "报告题目", "skipped", REASON_NO_SYNTHESIS,
    )
    assert _events(service, "ask_report_followup") == [{
        "kind": "ask_report_followup", "notebook_id": "nb",
        "receipt_status": "skipped", "reason_code": REASON_NO_SYNTHESIS,
        "references": 1, "restored": 0, "gone": 0, "over_budget": 0,
        "sections_total": 1, "sections_read": 0, "summary_truncated": False,
    }]


def test_a_conflict_is_the_receipt_reason_and_nothing_is_read_back():
    calls: list = []
    service = _service(calls, visible=())

    response = _ask(service)

    assert "restore" not in calls
    assert response.linked_report.status == "skipped"
    assert response.linked_report.reason_code == REASON_SCOPE_SOURCES
    assert response.linked_report.reason == (
        "本轮不读取这份报告：它引用的 1 份资料不在当前检索范围内"
    )


def test_no_report_means_no_read_no_receipt_and_an_unchanged_answer():
    calls: list = []
    service = _service(calls)

    response = _ask(service, report_id=None)

    assert calls == []
    assert response.linked_report is None
    assert "linked_report" not in response.model_dump_json()
    assert _events(service, "ask_report_followup") == []


def test_extension_engine_reads_nothing_and_still_answers():
    """An extension engine cannot read a report (D7): its turn answers as
    usual with ``engine_unsupported`` -- decided before a single read: no
    report row, no owner, no participant, no restore."""
    from app.extensions.bootstrap import build_extension_runtime
    from tests.test_ask_engine_plugin import _all_live, _bundle, _Provider

    runtime = build_extension_runtime((_bundle("alpha", _Provider("alpha.kg")),))
    calls: list = []
    service = _service(
        calls,
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _nb: ("nb",),
        ask_engine_visible_sources=lambda _nb: ("source-1",),
        ask_engine_hidden_sources=lambda _nb, _actor: (),
    )
    service.retrieval.federated_retrieve = lambda *_args, **_kwargs: []
    service.retrieval.federated_retrieve_elements = lambda *_args, **_kwargs: []
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.citation_source_info = lambda _ids: {}
    service.collection_catalog = SimpleNamespace(collection_map_text=lambda _nb: "")

    response = _ask(service, mode="alpha.kg")

    assert calls == []
    assert response.answer_id == "answer-1" and response.answer
    receipt = response.linked_report
    assert (receipt.status, receipt.reason_code, receipt.title) == (
        "skipped", REASON_ENGINE_UNSUPPORTED, "",
    )
    assert receipt.reason == (
        "本轮不读取这份报告：当前回答方式不读取关联报告，请改用「通用问答」或「逐步推理」"
    )


def test_report_link_check_judges_like_the_run_but_never_restores():
    calls: list = []
    service = _service(calls)

    check = service.report_link_check(
        "nb", REPORT_ID, user_id="user", mode_supported=True,
    )
    assert (check.readable, check.title, check.reason_code) == (True, "报告题目", "")
    assert calls == ["read_report", "source_owners"]

    calls.clear()
    unsupported = service.report_link_check(
        "nb", REPORT_ID, user_id="user", mode_supported=False,
    )
    assert calls == []
    assert (unsupported.readable, unsupported.reason_code) == (
        False, REASON_ENGINE_UNSUPPORTED,
    )


@pytest.mark.parametrize(("stage", "error", "title"), [
    ("read_report", OSError("/srv/private/report.db is gone"), ""),
    ("source_owners", OSError("/srv/private/source.db is gone"), "报告题目"),
    ("restore", sqlite3.OperationalError("database is locked: secret"), "报告题目"),
])
def test_a_data_side_failure_does_not_read_the_report_and_the_answer_stands(
    stage, error, title,
):
    """D6: any data read failing -- the report row, an owner map, the restore
    -- leaves the report unread (``read_failed``, the title once it was
    known), the answer is produced as usual, and the event names the error's
    class and nothing of its text."""
    calls: list = []
    service = _service(calls, fail={stage: error})

    response = _ask(service)

    assert response.conclusion and response.answer_id == "answer-1"
    receipt = response.linked_report
    assert (receipt.status, receipt.reason_code, receipt.title) == (
        "skipped", REASON_READ_FAILED, title,
    )
    assert _events(service, "ask_report_followup_failed") == [{
        "kind": "ask_report_followup_failed", "notebook_id": "nb",
        "error_type": type(error).__name__, "status": "failed",
    }]
    assert "secret" not in json.dumps(service.events, ensure_ascii=False)
    assert "/srv/private" not in json.dumps(service.events, ensure_ascii=False)


def test_an_unwired_service_answers_with_read_failed():
    calls: list = []
    service = _service(calls, report_followup_readers=None)

    response = _ask(service)

    assert response.linked_report.reason_code == REASON_READ_FAILED
    assert _events(service, "ask_report_followup_failed")[0]["error_type"] == (
        "RuntimeError"
    )


@pytest.mark.parametrize(("stage", "error"), [
    ("read_report", AskCancelled()),
    ("source_owners", AskCancelled()),
    ("restore", AskCancelled()),
    ("restore", RetrievalControlError("participant override mismatch")),
    ("read_report", RetrievalControlError("participant override mismatch")),
    ("restore", ReportRestoreScopeError("no retrieval ceiling is installed")),
])
def test_stops_control_errors_and_wiring_faults_are_never_swallowed(stage, error):
    """A Stop, a participant-override control error and a restore outside its
    frame (a wiring fault, not a data state) reach the caller: none of them
    may become "the report was not read"."""
    calls: list = []
    service = _service(calls, fail={stage: error})

    with pytest.raises(type(error)):
        _ask(service)
    assert _events(service, "ask_report_followup_failed") == []


def test_the_retrieval_only_output_carries_the_receipt_too():
    """``ask_evidence`` enters the same engine scope (the report is judged
    and read back there) and stamps the same receipt on its way out; what
    the chunk engine assembles in between is not this test's subject."""
    from app.models.ask import AskEvidence

    calls: list = []
    service = _service(calls)
    service._chunk_evidence = lambda *_args, **_kwargs: AskEvidence(
        mode="chunk", evidence_kind="retrieval",
    )

    evidence = service.ask_evidence(
        "nb", AskRequest(question="q", report_id=REPORT_ID), user_id="user",
    )

    assert calls == ["read_report", "source_owners", "restore"]
    assert evidence.linked_report.status == "skipped"
    assert evidence.linked_report.reason_code == REASON_NO_SYNTHESIS
    assert len(_events(service, "ask_report_followup")) == 1


# --------------------------------------------------------- idempotent retry
def test_the_same_key_with_another_report_is_another_request(world):
    """``client_request_id`` names ONE request: the same question about
    another report -- or about none -- is refused as a reused key, never
    attached to the first job's answer."""
    store = world.repo._runtime.ask_state
    service = world.repo._runtime.ask_service()
    first = AskRequest(
        question="Q?", mode="chunk", report_id="rep-1", client_request_id="key-1",
    )
    with as_request_user(world.alice):
        job_id, _conversation, attached = store.begin_or_attach_durable_job(
            world.nb, first, "chunk", world.alice.id, submitted_via="mcp",
        )
        assert not attached
        for report_id in ("rep-2", None):
            with pytest.raises(AskRequestKeyReused) as caught:
                service.ask_current_with_job(world.nb, AskRequest(
                    question="Q?", mode="chunk", report_id=report_id,
                    client_request_id="key-1",
                ), submitted_via="mcp")
            assert caught.value.reason == "question"
        # The same request is the same request.
        service._require_same_request(job_id, first, "mcp", None)


# ------------------------------------------------------------ understanding
class _RecordingUnderstanding:
    configured = True
    model = "fake-intent"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **_kwargs):
        self.prompts.append(messages[0]["content"])
        return json.dumps({
            "normalized_question": "两种工艺的依据", "intent_type": "explain",
            "result_scope": "ranked", "completeness_required": False,
            "entities": [], "mandatory_topics": [], "ambiguities": [],
            "needs_clarification": False, "confidence": 0.9,
        })


def _preview(world, question, report_id=None, **scopes):
    service = world.repo._runtime.ask_service()
    with as_request_user(world.alice):
        return service.preview_reasoning_intent(
            world.nb, question, "", None, user_id=world.alice.id,
            report_id=report_id, **scopes,
        )


def test_understanding_sees_the_outline_of_a_readable_report(world):
    client = _RecordingUnderstanding()
    bind_chat_client(world.repo, "reasoning_agent", client)
    cases.insert_report(world, "rep-outline")

    _preview(world, "第二节的依据可靠吗？", "rep-outline")

    prompt = client.prompts[-1]
    assert (
        f"Attached report: 《{cases.TITLE}》\nReport sections: 1. 工艺甲 | 2. 工艺乙\n"
        f"{ATTACHED_REPORT_REFERENCE_RULE}\n\nUser request: 第二节的依据可靠吗？"
    ) in prompt


def test_understanding_sees_only_the_title_of_a_report_it_will_not_read(world):
    client = _RecordingUnderstanding()
    bind_chat_client(world.repo, "reasoning_agent", client)
    cases.insert_report(world, "rep-conflict")

    _preview(world, "第二节的依据可靠吗？", "rep-conflict", source_scope=SourceScope(
        mode="include", source_ids=["src-a"], narrowed=True,
    ))

    prompt = client.prompts[-1]
    assert (
        f"Attached report: 《{cases.TITLE}》 (it cannot be read in this turn; "
        "answer from the notebook evidence)\n"
    ) in prompt
    assert "工艺甲" not in prompt and "工艺乙" not in prompt


def test_understanding_without_a_report_is_unchanged(world):
    client = _RecordingUnderstanding()
    bind_chat_client(world.repo, "reasoning_agent", client)

    _preview(world, "第二节的依据可靠吗？")

    assert "Attached report" not in client.prompts[-1]
    assert ATTACHED_REPORT_REFERENCE_RULE not in client.prompts[-1]
    for keywords in (False, True):
        assert query_intent_prompt("q", keywords=keywords, report_block="") == (
            query_intent_prompt("q", keywords=keywords)
        )


AMBIGUOUS = "这个结论的依据是什么？"


def test_the_fallback_gate_stands_down_for_a_request_that_attached_a_report(world):
    """No understanding model: the deterministic wording rules decide. They
    ask about "这个" without a report and stand down with one -- decided by
    the request carrying a report, not by reading its text."""
    cases.insert_report(world, "rep-fallback")

    attached = _preview(world, AMBIGUOUS, "rep-fallback")
    plain = _preview(world, AMBIGUOUS)

    assert not attached.needs_clarification and attached.ambiguities == []
    assert plain.needs_clarification
    assert plain.ambiguities[0].id == "ambiguity-input"


def test_the_entry_gate_admits_a_report_question_without_reading_anything():
    calls: list = []
    service = _service(calls)
    history_reads: list = []
    service.ask_state.conversation_user_history = (
        lambda *args: history_reads.append(args) or ""
    )

    attached = service.resolve_reasoning_followup("nb", AskRequest(
        question=AMBIGUOUS, mode="reasoning", report_id=REPORT_ID,
    ))
    assert attached.gate_message == "" and attached.resolved_question == AMBIGUOUS
    assert calls == [] and history_reads == []

    plain = service.resolve_reasoning_followup("nb", AskRequest(
        question=AMBIGUOUS, mode="reasoning",
    ))
    assert plain.gate_message.startswith("问题仍有关键歧义")


def test_the_compatibility_gate_takes_the_outline():
    payload = AskRequest(question=AMBIGUOUS, mode="reasoning")
    with pytest.raises(ValueError):
        AskService._confirmed_reasoning_intent(payload, "")
    contract = AskService._confirmed_reasoning_intent(
        payload, "", linked_report="Attached report: 《报告题目》",
    )
    assert not contract.needs_clarification


@pytest.mark.parametrize("question", [AMBIGUOUS, "分析一下"])
def test_plan_and_gate_rules_yield_only_to_an_attached_report(question):
    assert plan_query_intent(None, question)["needs_clarification"]
    assert not plan_query_intent(
        None, question, linked_report="Attached report: 《t》",
    )["needs_clarification"]
    assert followup_gate(question, "", max_topics=1)[1]
    seed, message = followup_gate(
        question, "", max_topics=1, linked_report="Attached report: 《t》",
    )
    assert message == "" and not seed["needs_clarification"]
