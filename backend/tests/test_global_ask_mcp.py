"""Global MCP boundary and resumable transport contracts (no ambient services)."""
from types import SimpleNamespace
import json

import pytest

from app.api.mcp_tools import global_ask as module
from app.api.mcp_tools import session as session_module
from app.api.mcp_tools._shared import RESULT_LIMIT, TOTAL_TEXT_LIMIT
from app.models.ask import AskResponse
from app.models.global_ask import GlobalAskJob, GlobalAskSkippedNotebook
from tests.test_memory_mcp import OfficialMcpClient, _payload, mcp_env


class Capture:
    def __init__(self):
        self.handlers = {}

    def tool(self, *, description):
        def register(handler):
            self.handlers[handler.__name__] = handler
            return handler
        return register


def job(*, answer="", citations=None, mode=None, trace=None):
    """A REAL ``GlobalAskJob``, not a job-shaped dict.

    ``_job_page`` is only ever handed a real job in production (every
    ``GlobalAskService`` method it reads returns one), so a test fixture that
    hands it a loose dict was paying for an adapter in production code that
    only the tests ever exercised -- and an adapter that reads missing keys as
    ``None`` is exactly the shape that hides a projection regression. Building
    the real model here costs the required fields (``question``/``created_at``
    on the job, ``label``/``location_label`` on each citation) and buys the
    guarantee that what the page reads is what a live job carries.
    """
    payload = {
        "job_id": "job-a", "conversation_id": "conversation-a", "status": "done",
        "question": "问题", "created_at": "2026-09-20T00:00:00+00:00",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": ["nb-a"], "searched_notebook_ids": ["nb-a"],
        "cited_notebook_ids": ["nb-a"], "error": None,
        "response": {
            "answer_id": "answer-a", "question": "问题", "answer": answer,
            "grounded": True, "citations": citations or [],
            "created_at": "2026-09-20T00:00:00+00:00",
            "notebook_scope": {"mode": "all", "notebook_ids": []},
            "resolved_notebook_ids": ["nb-a"],
            "searched_notebook_ids": ["nb-a"], "cited_notebook_ids": ["nb-a"],
        },
    }
    if mode is not None:
        payload["mode"] = mode
    if trace is not None:
        payload["trace"] = trace
    return GlobalAskJob.model_validate(payload)


def _citation(**overrides):
    row = {
        "label": "来源", "source_id": "s-a", "element_id": "e-a",
        "location_label": "第 1 节", "quoted_span": "原文",
        "notebook_id": "nb-a",
    }
    row.update(overrides)
    return row


def test_a_turn_answered_by_the_shared_engine_reads_back(monkeypatch):
    """新形状(``answer``)与旧形状(``response``)经同一个接缝读出来。

    D1-4 之后新作业写的是标准 ``AskResponse``,旧行仍然只有 ``response``。任何一
    个形状读空,MCP 客户端看到的就是一次「完成了但没有答案」的提问。
    """
    legacy = job(answer="旧形状答案", citations=[_citation()])
    current = job()
    current.response = None
    current.answer = AskResponse.model_validate({
        "answer_id": "", "conclusion": "结论", "answer": "新形状答案",
        "grounded": True, "citations": [_citation()],
    })

    for value, expected in ((legacy, "旧形状答案"), (current, "新形状答案")):
        page = module._job_page(value)
        assert page["answer"] == expected
        assert page["grounded"] is True
        assert [row["notebook_id"] for row in page["citations"]] == ["nb-a"]
        assert page["total_citations"] == 1


def test_trace_pages_independently_with_its_own_cursor():
    """Reasoning trace steps page under ``trace`` with their own offset/next_offset,

    independent of the answer/citation/coverage cursors -- same resumable
    semantics (an explicit ``None`` at the end, never a missing key), just
    nested rather than flat (see ``_job_page``'s own note on why).
    """
    steps = [
        {"step_type": "plan" if i % 2 else "retrieve", "summary": f"step {i}",
         "detail": {"i": i}, "duration_ms": i}
        for i in range(45)
    ]
    value = job(answer="answer", mode="reasoning", trace=steps)
    offset = 0
    collected: list = []
    pages = 0
    while True:
        page = module._job_page(value, trace_offset=offset)
        assert page["trace"]["total"] == 45
        assert page["trace"]["offset"] == offset
        collected.extend(page["trace"]["steps"])
        pages += 1
        if page["trace"]["next_offset"] is None:
            break
        assert page["trace"]["next_offset"] > offset
        offset = page["trace"]["next_offset"]
    assert pages > 1, "45 steps must not fit in a single page (RESULT_LIMIT=20)"
    assert [step["summary"] for step in collected] == [f"step {i}" for i in range(45)]
    assert collected[0]["kind"] == "retrieve" and collected[0]["duration_ms"] == 0
    assert collected[1]["kind"] == "plan"


def test_trace_is_empty_for_a_chunk_job_with_no_steps():
    page = module._job_page(job(answer="answer"))
    assert page["trace"] == {"steps": [], "offset": 0, "next_offset": None, "total": 0}


def test_coverage_retains_every_skip_and_degraded_receipt():
    value = job(answer="partial answer")
    value.skipped_notebooks = [GlobalAskSkippedNotebook(notebook_id="nb-b", reason="检索超时，请重试。")]
    value.degraded_notebook_ids = ["nb-a"]
    page = module._job_page(value)
    assert page["coverage"]["skipped_notebooks"] == [
        row.model_dump(mode="json") for row in value.skipped_notebooks
    ]
    assert page["coverage"]["degraded_notebook_ids"] == ["nb-a"]
    assert page["coverage"]["skipped"] == page["coverage"]["degraded"] == 1
    assert page["coverage_offset"] == 0 and page["next_coverage_offset"] is None


@pytest.mark.parametrize("long_ids", [False, True])
@pytest.mark.parametrize("status", ["running", "done", "cancelled"])
def test_coverage_pages_reassemble_all_32_notebook_identities_under_the_byte_budget(long_ids, status):
    ids = [f"{index:03d}-" + ("库" * 196 if long_ids else "library") for index in range(32)]
    value = job(answer="跨库答案与原文证据。" * 500)
    value.status = status
    value.resolved_notebook_ids = ids
    value.searched_notebook_ids = ids[:23]
    value.skipped_notebooks = [GlobalAskSkippedNotebook(notebook_id=nb, reason="检索超时，请重试。") for nb in ids]
    value.degraded_notebook_ids = ids[:23]
    offset = 0
    skipped, degraded, page_counts = [], [], []
    while True:
        page = module._job_page(value, coverage_offset=offset)
        assert page["status"] == status
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        coverage = page["coverage"]
        assert (coverage["resolved"], coverage["searched"], coverage["skipped"], coverage["degraded"]) == (32, 23, 32, 23)
        assert page["coverage_offset"] == offset
        skipped.extend(coverage["skipped_notebooks"])
        degraded.extend(coverage["degraded_notebook_ids"])
        count = max(len(coverage["skipped_notebooks"]), len(coverage["degraded_notebook_ids"]))
        page_counts.append(count)
        assert 0 < count <= RESULT_LIMIT
        if page["next_coverage_offset"] is None:
            break
        assert page["next_coverage_offset"] == offset + count
        offset = page["next_coverage_offset"]
    assert skipped == [row.model_dump(mode="json") for row in value.skipped_notebooks]
    assert degraded == value.degraded_notebook_ids
    if long_ids:
        assert page_counts[0] < RESULT_LIMIT


def test_coverage_cursor_advances_when_only_the_longer_list_has_remaining_items():
    value = job()
    value.skipped_notebooks = [GlobalAskSkippedNotebook(notebook_id="skipped", reason="请重试。")]
    value.degraded_notebook_ids = [f"nb-{index}" for index in range(32)]
    first = module._job_page(value)
    assert first["next_coverage_offset"] == RESULT_LIMIT
    last = module._job_page(value, coverage_offset=first["next_coverage_offset"])
    assert last["coverage"]["skipped_notebooks"] == []
    assert first["coverage"]["degraded_notebook_ids"] + last["coverage"]["degraded_notebook_ids"] == value.degraded_notebook_ids
    assert last["next_coverage_offset"] is None
    exhausted = module._job_page(value, coverage_offset=100)
    assert exhausted["coverage"]["skipped_notebooks"] == exhausted["coverage"]["degraded_notebook_ids"] == []
    assert exhausted["next_coverage_offset"] is None


@pytest.fixture
def adapter(monkeypatch):
    principal = SimpleNamespace(
        token_id="token-a", owner_id="owner-a", profile_name="Agent",
        scopes=["ask:execute", "knowledge:read"], notebook_ids=["nb-a"],
    )
    repo = SimpleNamespace(refresh_agent_principal=lambda _: principal)
    service = SimpleNamespace()
    monkeypatch.setattr(module, "_live_principal", lambda _: principal)
    monkeypatch.setattr(module, "global_ask_service", lambda: service)
    monkeypatch.setattr(module, "_record_agent_call", lambda *args: None)

    async def run(ctx, work, *, label):
        return work()

    monkeypatch.setattr(module, "_run_with_progress", run)
    capture = Capture()
    module.register_global_ask_tools(capture, lambda: repo)
    return capture.handlers, principal, repo, service


@pytest.mark.anyio
async def test_start_without_selection_preserves_scope_and_passes_live_authority(adapter):
    handlers, principal, repo, service = adapter
    seen = []

    def start(payload, **kwargs):
        seen.append((payload, kwargs))
        return job()

    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    result = await handlers["ask_global"](
        "各项目有哪些结论？", ctx,
        notebook_scope={"mode": "include", "notebook_ids": []},
        client_request_id="retry-key",
    )
    assert result["job_id"] == "job-a"
    assert result["conversation_path"] == "/ask?conversation_id=conversation-a"
    payload, kwargs = seen[0]
    assert payload.notebook_scope.mode == "all"
    assert payload.client_request_id == "retry-key"
    assert kwargs["user_id"] == "owner-a"
    assert kwargs["allowed_notebook_ids"] == ["nb-a"]
    assert kwargs["submitted_via"] == "mcp"
    assert kwargs["authority_check"]() == ["nb-a"]
    principal.notebook_ids = ["nb-b"]
    assert kwargs["authority_check"]() == ["nb-b"]
    principal.scopes = ["knowledge:read"]
    with pytest.raises(PermissionError):
        kwargs["authority_check"]()


@pytest.mark.anyio
async def test_followup_omitted_scope_is_not_replaced_with_all(adapter):
    handlers, _, _, service = adapter
    seen = []
    service.start = lambda payload, **kwargs: (seen.append(payload), job())[1]
    await handlers["ask_global"]("继续比较", None, conversation_id="conversation-a")
    assert seen[0].notebook_scope is None
    assert seen[0].conversation_id == "conversation-a"


@pytest.mark.anyio
async def test_ask_global_passes_mode_through_to_start_and_the_page(adapter):
    """D1-5: ``mode``/``retrieval_effort`` reach ``GlobalAskRequest`` and the
    returned job's ``mode`` surfaces on the page, byte for byte."""
    handlers, _, _, service = adapter
    seen = []

    def start(payload, **kwargs):
        seen.append(payload)
        return job(mode=payload.mode)

    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    result = await handlers["ask_global"](
        "问题", ctx, mode="chunk", retrieval_effort="standard",
    )
    assert seen[0].mode == "chunk"
    assert seen[0].retrieval_effort == "standard"
    assert result["mode"] == "chunk"


@pytest.mark.anyio
async def test_ask_global_unknown_mode_surfaces_the_core_error_verbatim(adapter):
    """An unrecognized/plugin mode's 422 keeps the same shape every other
    ``GlobalAskError`` gets on this surface: a ``_ToolInputError`` carrying
    the core's own Chinese copy, not a raw exception dump."""
    from app.services.global_ask import GlobalAskError

    handlers, _, _, service = adapter

    def start(payload, **kwargs):
        raise GlobalAskError(422, "不支持的问答引擎，请刷新页面后重试。")

    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    with pytest.raises(module._ToolInputError) as error:
        await handlers["ask_global"]("问题", ctx, mode="bogus")
    assert str(error.value) == "不支持的问答引擎，请刷新页面后重试。"


@pytest.mark.anyio
async def test_ask_global_reasoning_auto_confirms_a_clear_question(adapter):
    """A clear reasoning question runs the understanding pass in-call and
    submits with a confirmed intent -- no clarification pause, no second
    round trip, matching how MCP ``ask_notebook`` treats a clear question."""
    handlers, principal, _, service = adapter
    from app.models.ask import QueryIntentContract

    def preview_intent(payload, *, user_id, allowed_notebook_ids, authority_check):
        return QueryIntentContract(
            objective=payload.question, resolved_question=payload.question,
            needs_clarification=False,
        )

    seen = []

    def start(payload, **kwargs):
        seen.append(payload)
        return job(mode="reasoning")

    service.preview_intent = preview_intent
    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    result = await handlers["ask_global"]("解释一下这份研究的方法论", ctx, mode="reasoning")
    assert result["status"] == "done"
    assert seen[0].intent is not None
    assert seen[0].intent.resolved_question == "解释一下这份研究的方法论"


@pytest.mark.anyio
async def test_ask_global_reasoning_needs_clarification_creates_nothing(adapter):
    """An ambiguous reasoning question returns the structured pause instead
    of a job -- no ``service.start`` call, no session-scoped handle (there is
    none on this surface; see ``_needs_clarification_payload``'s docstring)."""
    handlers, principal, _, service = adapter
    from app.models.ask import QueryIntentContract, QueryIntentAmbiguity

    def preview_intent(payload, *, user_id, allowed_notebook_ids, authority_check):
        return QueryIntentContract(
            objective=payload.question, resolved_question=payload.question,
            needs_clarification=True,
            ambiguities=[QueryIntentAmbiguity(id="a1", question="具体指哪一个项目？")],
        )

    def start(payload, **kwargs):
        pytest.fail("an ambiguous reasoning question must not create a job")

    service.preview_intent = preview_intent
    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    result = await handlers["ask_global"]("它的结论是什么", ctx, mode="reasoning")
    assert result["status"] == "needs_clarification"
    assert result["intent"]["ambiguities"][0]["question"] == "具体指哪一个项目？"
    assert "ask_global" in result["next_step"]


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["ask_global", "get_global_ask", "cancel_global_ask"])
async def test_answer_operations_require_both_scopes_before_service_access(adapter, name):
    handlers, principal, _, _ = adapter
    principal.scopes = ["knowledge:read"]
    arguments = {"question": "问题"} if name == "ask_global" else {"job_id": "job-a"}
    with pytest.raises(PermissionError):
        await handlers[name](ctx=None, **arguments)


@pytest.mark.anyio
async def test_read_and_cancel_forward_current_owner_and_allowlist(adapter):
    handlers, principal, _, service = adapter
    calls = []
    service.get_job = lambda job_id, **kw: (calls.append((job_id, kw)), job())[1]
    service.cancel = service.get_job
    await handlers["get_global_ask"]("job-a", None)
    principal.notebook_ids = ["nb-b"]
    await handlers["cancel_global_ask"]("job-a", None)
    assert calls == [
        ("job-a", {"user_id": "owner-a", "allowed_notebook_ids": ["nb-a"]}),
        ("job-a", {"user_id": "owner-a", "allowed_notebook_ids": ["nb-b"]}),
    ]


@pytest.mark.anyio
async def test_get_continues_coverage_independently_and_rejects_negative_offset(adapter):
    handlers, _, _, service = adapter
    saved = job(answer="完整答案")
    saved.degraded_notebook_ids = [f"nb-{index}" for index in range(32)]
    calls = []
    service.get_job = lambda *_args, **_kw: calls.append(True) or saved
    first = await handlers["get_global_ask"]("job-a", None)
    last = await handlers["get_global_ask"]("job-a", None, coverage_offset=first["next_coverage_offset"])
    assert first["answer"] == last["answer"] == "完整答案"
    assert last["coverage"]["degraded_notebook_ids"] == saved.degraded_notebook_ids[RESULT_LIMIT:]
    assert last["next_coverage_offset"] is None
    with pytest.raises(ValueError, match="分页位置不能小于零"):
        await handlers["get_global_ask"]("job-a", None, coverage_offset=-1)
    assert len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("error", [RuntimeError, ValueError, PermissionError])
async def test_unexpected_service_errors_never_echo_private_details(adapter, error):
    handlers, _, _, service = adapter

    def fail(*_args, **_kwargs):
        raise error("private-sentinel SQL credential path")

    service.get_job = fail
    with pytest.raises(ValueError, match="请稍后重试") as caught:
        await handlers["get_global_ask"]("job-a", None)
    assert "private-sentinel" not in str(caught.value)


def test_large_cjk_answer_and_citations_are_resumable_without_skipped_identity():
    full_answer = "全局原文证据😀\n" * 1500
    refs = [_citation(
        notebook_id=f"nb-{i}", source_id=f"source-{i}",
        element_id=f"element-{i}", quoted_span="引用内容" * 500,
        source_file_name="很长的标题" * 100,
    ) for i in range(43)]
    saved = job(answer=full_answer, citations=refs)
    answer_offset = citation_offset = 0
    collected_text = []
    collected_refs = []
    while True:
        page = module._job_page(saved, answer_offset, citation_offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        assert page["job_id"] == "job-a"
        assert page["answer_id"] == "answer-a"
        collected_text.append(page["answer"])
        collected_refs.extend(page["citations"])
        answer_offset += len(page["answer"])
        citation_offset += len(page["citations"])
        if page["next_answer_offset"] is None and page["next_citation_offset"] is None:
            break
        assert page["answer"] or page["citations"]
    assert "".join(collected_text) == full_answer
    assert module._citation_keys(collected_refs) == module._citation_keys(refs)


@pytest.mark.anyio
async def test_citation_original_text_pages_and_knowledge_only_scope(adapter):
    handlers, principal, _, service = adapter
    principal.scopes = ["knowledge:read"]
    full_text = "原文😀\n" * 1700
    calls = []

    def cited(job_id, element_id, **kw):
        calls.append((job_id, element_id, kw))
        return {"id": element_id, "source_id": "source-a", "text": full_text,
                "element_type": "text", "location_label": "第三页"}

    service.cited_element = cited
    offset, pages = 0, []
    while True:
        page = await handlers["get_global_cited_element"]("job-a", "element-a", None, offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == full_text
    assert all(call == ("job-a", "element-a", {
        "user_id": "owner-a", "allowed_notebook_ids": ["nb-a"],
    }) for call in calls)


@pytest.mark.anyio
async def test_discovery_search_reaches_beyond_first_twenty_allowlisted_notebooks(monkeypatch):
    principal = SimpleNamespace(
        owner_id="owner-a", profile_name="Agent", default_notebook_id="nb-0",
        notebook_ids=[f"nb-{index}" for index in range(55)],
    )
    repo = SimpleNamespace(
        user_can_read_notebook=lambda notebook_id, _: notebook_id != "nb-2",
        get_notebook=lambda notebook_id: SimpleNamespace(
            id=notebook_id, name=notebook_id, purpose="project", tier="personal",
            access="private", counts={},
        ),
    )
    monkeypatch.setattr(session_module, "_live_principal", lambda _: principal)

    async def run(ctx, work, *, label):
        return work()

    monkeypatch.setattr(session_module, "_run_with_progress", run)
    capture = Capture()
    session_module.register_session_tools(capture, lambda: repo)
    offset, ids = 0, []
    while True:
        page = await capture.handlers["list_notebooks"](None, offset=offset)
        ids.extend(row["notebook_id"] for row in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert ids == [item for item in principal.notebook_ids if item != "nb-2"]
    page = await capture.handlers["list_notebooks"](None, query="NB-54")
    assert [row["notebook_id"] for row in page["items"]] == ["nb-54"]
    assert page["next_offset"] is None


@pytest.mark.anyio
async def test_official_client_global_tools_need_no_selected_notebook(mcp_env, monkeypatch):
    notebook_id = mcp_env["notebook"].id
    saved = job(answer="跨库答案")
    saved.resolved_notebook_ids = [notebook_id]
    calls = []

    def start(payload, **kwargs):
        calls.append(kwargs)
        assert kwargs["authority_check"]() == [notebook_id]
        return saved

    service = SimpleNamespace(
        start=start, get_job=lambda *_args, **_kw: saved,
        cancel=lambda *_args, **_kw: saved.model_copy(update={"status": "cancelled"}),
        cited_element=lambda *_args, **_kw: {
            "id": "element-a", "source_id": "source-a", "text": "引用原文",
        },
    )
    monkeypatch.setattr(module, "global_ask_service", lambda: service)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        started = _payload(await client.call("ask_global", {"question": "跨库结论是什么？"}))
        assert started["job_id"] == "job-a"
        result = _payload(await client.call("get_global_ask", {"job_id": "job-a", "coverage_offset": 1}))
        assert result["answer"] == "跨库答案"
        assert result["coverage_offset"] == 1 and result["next_coverage_offset"] is None
        element = _payload(await client.call("get_global_cited_element", {
            "job_id": "job-a", "element_id": "element-a",
        }))
        assert element["text"] == "引用原文"
        cancelled = _payload(await client.call("cancel_global_ask", {"job_id": "job-a"}))
        assert cancelled["status"] == "cancelled"
    assert calls[0]["allowed_notebook_ids"] == [notebook_id]
