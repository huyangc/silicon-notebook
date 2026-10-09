"""Global MCP boundary and resumable transport contracts (no ambient services)."""
from types import SimpleNamespace
import json

import pytest

from app.api.mcp_tools import ask as ask_module
from app.api.mcp_tools import citations as citations_module
from app.api.mcp_tools import global_ask as module
from app.api.mcp_tools.ask_pages import answer_page
from app.api.mcp_tools import session as session_module
from app.api.mcp_tools._shared import (
    OUTPUT_MAPPING_LIMIT, RESULT_LIMIT, TOTAL_TEXT_LIMIT, AgentToolError,
)
from app.api.mcp_tools.refs import global_element_ref
from app.models.ask import AnswerAnchor, AskResponse
from app.models.global_ask import GlobalAskJob, GlobalAskSkippedNotebook
from tests.test_memory_mcp import OfficialMcpClient, _payload, mcp_env


class Capture:
    def __init__(self):
        self.handlers = {}

    def tool(self, *, description, tier=None):
        def register(handler):
            self.handlers[handler.__name__] = handler
            return handler
        return register


def _page(value, *offsets, **named):
    """The MCP answer page of one global job (``ask`` / ``get_ask``)."""
    return answer_page(module.global_view(value), *offsets, **named)


def job(*, answer="", citations=None, mode=None, trace=None):
    """A REAL ``GlobalAskJob``, not a job-shaped dict.

    ``global_view`` is only ever handed a real job in production (every
    ``GlobalAskService`` method it reads returns one), so a test fixture that
    hands it a loose dict was paying for an adapter in production code that
    only the tests ever exercised -- and an adapter that reads missing keys as
    ``None`` is exactly the shape that hides a projection regression. Building
    the real model here costs the required fields (``question``/``created_at``
    on the job, ``label``/``location_label`` on each citation) and buys the
    guarantee that what the page reads is what a live job carries.
    """
    payload = {
        "job_id": "gask-a", "conversation_id": "conversation-a", "status": "done",
        "submitted_via": "mcp",
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
        page = _page(value)
        assert page["answer"] == expected
        assert page["grounded"] is True
        assert [row["notebook_id"] for row in page["citations"]] == ["nb-a"]
        assert page["total_citations"] == 1


def test_trace_pages_independently_with_its_own_cursor():
    """Reasoning trace steps page under ``trace`` with their own offset/next_offset,

    independent of the answer/citation/coverage cursors -- same resumable
    semantics (an explicit ``None`` at the end, never a missing key), just
    nested rather than flat.
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
        page = _page(value, trace_offset=offset)
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


@pytest.mark.anyio
async def test_a_trace_summary_carrying_a_whole_question_is_bounded_and_flagged(adapter):
    """A ``search_chunks`` step can embed the full 4,000-character question in
    its summary -- alone larger than the page budget. The step's summary is
    capped and flagged (like its detail), so ``ask`` and ``get_ask`` still
    return the finished answer instead of ``internal``."""
    handlers, _, _, service = adapter
    question = "问" * 4_000
    steps = [
        {"step_type": "search_chunks", "summary": f"检索：{question}",
         "detail": {"query": question}, "duration_ms": 1},
        {"step_type": "plan", "summary": "short", "detail": {}, "duration_ms": 2},
    ]
    finished = job(answer="终态答案", mode="reasoning", trace=steps)
    service.start = lambda payload, **kwargs: finished
    service.get_job = lambda job_id, **kwargs: finished
    asked = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    read = await handlers["get_ask"]("gask-a", None)
    for page in (asked, read):
        assert page["answer"] == "终态答案"
        first, second = page["trace"]["steps"]
        assert first["summary_truncated"] is True
        assert first["detail_truncated"] is True
        assert len(first["summary"]) < len(steps[0]["summary"])
        assert second["summary"] == "short" and second["summary_truncated"] is False
        assert page["trace"]["next_offset"] is None


def test_trace_is_empty_for_a_chunk_job_with_no_steps():
    page = _page(job(answer="answer"))
    assert page["trace"] == {"steps": [], "offset": 0, "next_offset": None, "total": 0}


def test_coverage_retains_every_skip_and_degraded_receipt():
    value = job(answer="partial answer")
    value.skipped_notebooks = [GlobalAskSkippedNotebook(notebook_id="nb-b", reason="检索超时，请重试。")]
    value.degraded_notebook_ids = ["nb-a"]
    page = _page(value)
    assert page["coverage"]["skipped_notebooks"] == [
        row.model_dump(mode="json") for row in value.skipped_notebooks
    ]
    assert page["coverage"]["degraded_notebook_ids"] == ["nb-a"]
    assert page["coverage"]["skipped"] == page["coverage"]["degraded"] == 1
    assert page["coverage"]["offset"] == 0 and page["next_coverage_offset"] is None


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
        page = _page(value, coverage_offset=offset)
        assert page["status"] == module.JOB_STATUS[status]
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        coverage = page["coverage"]
        assert (coverage["resolved"], coverage["searched"], coverage["skipped"], coverage["degraded"]) == (32, 23, 32, 23)
        assert page["coverage"]["offset"] == offset
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
    first = _page(value)
    assert first["next_coverage_offset"] == RESULT_LIMIT
    last = _page(value, coverage_offset=first["next_coverage_offset"])
    assert last["coverage"]["skipped_notebooks"] == []
    assert first["coverage"]["degraded_notebook_ids"] + last["coverage"]["degraded_notebook_ids"] == value.degraded_notebook_ids
    assert last["next_coverage_offset"] is None
    exhausted = _page(value, coverage_offset=100)
    assert exhausted["coverage"]["skipped_notebooks"] == exhausted["coverage"]["degraded_notebook_ids"] == []
    assert exhausted["next_coverage_offset"] is None


class _MemoryHandles:
    """In-memory ``ask_intent_handles`` for the adapter (the store has its own
    SQLite/PostgreSQL tests)."""

    def __init__(self):
        self.rows = {}

    def put_intent_handle(self, *, token, owner_id, **fields):
        self.rows[token] = {"token": token, "owner_id": owner_id, **fields}

    def get_intent_handle(self, token, *, owner_id):
        row = self.rows.get(token)
        if row is None or row["owner_id"] != owner_id:
            return None
        return {**row, "contract": row["contract"]}


class _FakeGlobalService(SimpleNamespace):
    """The global service seat of the adapter. ``ask`` re-reads the job with
    ``get_job`` when it delivers the answer (the delivery recheck); unless a
    test installs its own ``get_job``, that re-read hands back the last job
    the test's ``start`` / ``replay`` / ``wait`` returned."""

    _WRAPPED = ("start", "replay", "wait")

    def __init__(self, **fields):
        super().__init__(**fields)
        object.__setattr__(self, "last_job", None)

    def __setattr__(self, name, value):
        if name in self._WRAPPED and callable(value):
            inner = value

            def wrapped(*args, **kwargs):
                result = inner(*args, **kwargs)
                if isinstance(result, GlobalAskJob):
                    object.__setattr__(self, "last_job", result)
                return result

            value = wrapped
        object.__setattr__(self, name, value)

    def get_job(self, job_id, **kwargs):
        return self.last_job


@pytest.fixture
def adapter(monkeypatch):
    principal = SimpleNamespace(
        token_id="token-a", owner_id="owner-a", profile_name="Agent",
        default_notebook_id="nb-a",
        # ``ask`` alone starts/reads a global answer (no ``read``).
        scopes=["ask"], notebook_ids=["nb-a", "nb-b"],
    )
    repo = SimpleNamespace(refresh_agent_principal=lambda _: principal)
    # No replay by default (no earlier job under any key).
    service = _FakeGlobalService()
    service.replay = lambda *args, **kwargs: None
    handles = _MemoryHandles()
    monkeypatch.setattr(module, "_live_principal", lambda _: principal)
    monkeypatch.setattr(ask_module, "_live_principal", lambda _: principal)
    monkeypatch.setattr(module, "global_ask_service", lambda: service)
    monkeypatch.setattr(ask_module, "_record_agent_call", lambda *args: None)
    monkeypatch.setattr(ask_module, "ask_intent_handle_repository", lambda: handles)

    async def run(ctx, work, *, label, on_cancel=None):
        return work()

    monkeypatch.setattr(ask_module, "_run_with_progress", run)
    monkeypatch.setattr(citations_module, "_run_with_progress", run)
    capture = Capture()
    ask_module.register_ask_tools(capture, lambda: repo)
    # ``read_reference`` (kind ``gel``) is the global citation drill-down.
    citations_module.register_citation_tools(capture, lambda: repo)
    return capture.handlers, principal, repo, service


BOTH = ["nb-a", "nb-b"]


def _gel(job_id: str, element_id: str) -> str:
    return global_element_ref(job_id, element_id)


@pytest.mark.anyio
async def test_two_notebooks_start_a_global_job_with_live_authority(adapter):
    handlers, principal, repo, service = adapter
    seen = []

    def start(payload, **kwargs):
        seen.append((payload, kwargs))
        return job()

    service.start = start
    ctx = SimpleNamespace(session=SimpleNamespace())
    result = await handlers["ask"](
        "各项目有哪些结论？", ctx, notebooks=BOTH, mode="chunk",
        client_request_id="retry-key",
    )
    assert result["job_id"] == "gask-a"
    assert result["status"] == "answered"
    assert result["scope"]["kind"] == "global"
    payload, kwargs = seen[0]
    assert payload.notebook_scope.mode == "include"
    assert payload.notebook_scope.notebook_ids == BOTH
    assert payload.client_request_id == "retry-key"
    assert kwargs["user_id"] == "owner-a"
    assert kwargs["allowed_notebook_ids"] == BOTH
    assert kwargs["submitted_via"] == "mcp"
    assert kwargs["authority_check"]() == BOTH
    principal.notebook_ids = ["nb-b"]
    assert kwargs["authority_check"]() == ["nb-b"]
    principal.scopes = ["read"]
    with pytest.raises(AgentToolError) as refused:
        kwargs["authority_check"]()
    assert refused.value.code == "scope_missing"


@pytest.mark.anyio
async def test_a_running_global_job_is_waited_for_to_its_terminal_state(adapter):
    handlers, _, _, service = adapter
    running = job()
    running.status = "running"
    waited = []
    service.start = lambda payload, **kwargs: running
    service.wait = lambda job_id, **kwargs: (waited.append((job_id, kwargs)), job(answer="终态"))[1]
    result = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert result["answer"] == "终态"
    [(job_id, kwargs)] = waited
    stop = kwargs.pop("stop")
    assert job_id == "gask-a"
    assert kwargs == {"user_id": "owner-a", "allowed_notebook_ids": BOTH}
    # The waiter's abandon signal: set only when the call is cancelled.
    assert not stop.is_set()


@pytest.mark.anyio
async def test_cancelling_the_call_releases_a_global_waiter(adapter, monkeypatch):
    """A cancelled ``ask`` sets the wait's stop event, so the worker thread
    that was only waiting lets go instead of holding on to the job's end."""
    import threading

    import anyio

    from app.api.mcp_tools import _shared

    handlers, _, _, service = adapter
    monkeypatch.setattr(ask_module, "_run_with_progress", _shared._run_with_progress)
    running = job()
    running.status = "running"
    released = threading.Event()

    def wait(job_id, *, stop, **kwargs):
        if stop.wait(10):
            released.set()
            raise ask_module.AskWaitAbandoned()
        return job()

    service.start = lambda payload, **kwargs: running
    service.wait = wait
    ctx = SimpleNamespace(report_progress=lambda *a, **k: None)
    with anyio.move_on_after(0.3):
        await handlers["ask"]("问题", ctx, notebooks=BOTH, mode="chunk")
    assert released.wait(2), "the waiting worker thread was never released"


@pytest.mark.anyio
async def test_a_third_concurrent_waiter_on_one_job_is_busy(adapter):
    handlers, principal, _, service = adapter
    running = job()
    running.status = "running"
    service.start = lambda payload, **kwargs: running
    service.wait = lambda job_id, **kwargs: job()
    with ask_module._waiter_slot(principal.owner_id, "gask-a"):
        with ask_module._waiter_slot(principal.owner_id, "gask-a"):
            with pytest.raises(AgentToolError) as busy:
                await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert busy.value.code == "busy"
    # The slots are released afterwards.
    assert (await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk"))["status"]


@pytest.mark.anyio
async def test_a_global_conversation_inherits_its_scope(adapter):
    handlers, _, _, service = adapter
    seen = []
    service.start = lambda payload, **kwargs: (seen.append(payload), job())[1]
    await handlers["ask"]("继续比较", None, conversation_id="gconv-a", mode="chunk")
    assert seen[0].notebook_scope is None
    assert seen[0].conversation_id == "gconv-a"


@pytest.mark.anyio
async def test_global_mode_passes_through_and_plugin_engines_are_refused(adapter, monkeypatch):
    handlers, _, _, service = adapter
    seen = []

    def start(payload, **kwargs):
        seen.append(payload)
        return job(mode=payload.mode)

    service.start = start
    result = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert seen[0].mode == "chunk"
    assert result["mode"] == "chunk"
    monkeypatch.setattr(ask_module, "_plugin_mode", lambda mode: True)
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="corp.engine")
    assert refused.value.code == "invalid_argument"
    assert "单个笔记本" in refused.value.message
    assert len(seen) == 1


@pytest.mark.anyio
async def test_a_global_service_refusal_surfaces_its_copy_verbatim(adapter):
    """A ``GlobalAskError`` keeps the core's own Chinese copy, mapped by status."""
    from app.services.global_ask import GlobalAskError

    handlers, _, _, service = adapter

    def start(payload, **kwargs):
        raise GlobalAskError(422, "不支持的问答引擎，请刷新页面后重试。")

    service.start = start
    with pytest.raises(AgentToolError) as error:
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert error.value.code == "invalid_argument"
    assert str(error.value) == "[invalid_argument] 不支持的问答引擎，请刷新页面后重试。"


@pytest.mark.anyio
async def test_global_reasoning_auto_confirms_a_clear_question(adapter):
    """A clear reasoning question runs the understanding pass in-call and
    submits with a confirmed intent -- no clarification pause."""
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
    service.replay = lambda *a, **k: None
    service.start = start
    result = await handlers["ask"]("解释一下这份研究的方法论", None, notebooks=BOTH)
    assert result["status"] == "answered"
    assert seen[0].intent is not None
    assert seen[0].intent.resolved_question == "解释一下这份研究的方法论"


@pytest.mark.anyio
async def test_global_reasoning_retry_replays_before_previewing_again(adapter):
    """同一个 ``client_request_id`` 的重试先认出既有作业,不再跑一次问题理解。"""
    handlers, _, _, service = adapter
    previews, starts = [], []

    def preview_intent(payload, **kwargs):
        previews.append(payload)
        raise AssertionError("a replayed request must not be previewed again")

    def replay(payload, **kwargs):
        assert payload.client_request_id == "retry-1"
        return job(mode="reasoning")

    def start(payload, **kwargs):
        starts.append(payload)
        raise AssertionError("a replayed request must not start a second job")

    service.preview_intent = preview_intent
    service.replay = replay
    service.start = start
    result = await handlers["ask"](
        "解释一下这份研究的方法论", None, notebooks=BOTH, client_request_id="retry-1",
    )
    assert result["status"] == "answered"
    assert previews == [] and starts == []


@pytest.mark.anyio
async def test_global_reasoning_clarifies_by_a_stored_handle_and_resumes(adapter):
    """An ambiguous global reasoning question pauses with a server-side handle
    and creates no job; answering the handle (same question and scope)
    submits the stored contract as the confirmed intent. The handle is bound
    to its scope: the same token under another notebook set is refused."""
    handlers, principal, _, service = adapter
    from app.models.ask import QueryIntentContract, QueryIntentAmbiguity

    def preview_intent(payload, *, user_id, allowed_notebook_ids, authority_check):
        return QueryIntentContract(
            objective=payload.question, resolved_question=payload.question,
            needs_clarification=True,
            ambiguities=[QueryIntentAmbiguity(id="a1", question="具体指哪一个项目？")],
        )

    started = []
    service.preview_intent = preview_intent
    service.replay = lambda *a, **k: None
    service.start = lambda payload, **kwargs: (started.append(payload), job(mode="reasoning"))[1]
    paused = await handlers["ask"]("它的结论是什么", None, notebooks=BOTH)
    assert paused["status"] == "needs_clarification"
    assert paused["intent"]["ambiguities"][0]["question"] == "具体指哪一个项目？"
    assert paused["scope"] == {"kind": "global", "notebook_ids": BOTH}
    assert "ask" in paused["next_step"] and started == []
    reply = {"intent_token": paused["intent_token"], "answers": [{"id": "a1", "answer": "甲项目"}]}
    principal.notebook_ids = ["nb-a", "nb-b", "nb-c"]
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"](
            "它的结论是什么", None, notebooks=["nb-a", "nb-c"], intent=reply,
        )
    assert refused.value.code == "invalid_argument"
    result = await handlers["ask"]("它的结论是什么", None, notebooks=BOTH, intent=reply)
    assert result["status"] == "answered"
    [submitted] = started
    assert submitted.intent.contract.needs_clarification is True
    assert [(a.id, a.answer) for a in submitted.intent.answers] == [("a1", "甲项目")]


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["ask", "get_ask"])
async def test_answer_operations_require_the_ask_tier_before_service_access(adapter, name):
    handlers, principal, _, _ = adapter
    principal.scopes = ["read"]
    arguments = (
        {"question": "问题", "notebooks": BOTH, "mode": "chunk"} if name == "ask"
        else {"job_id": "gask-a"}
    )
    with pytest.raises(AgentToolError, match="缺少「问答」权限") as refused:
        await handlers[name](ctx=None, **arguments)
    assert refused.value.code == "scope_missing"


@pytest.mark.anyio
async def test_more_than_eight_notebooks_is_refused_before_any_work(adapter):
    handlers, _, _, service = adapter
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"](
            "问题", None, notebooks=[f"nb-{i}" for i in range(9)], mode="chunk",
        )
    assert refused.value.code == "invalid_argument"


@pytest.mark.anyio
async def test_cited_element_reads_require_the_read_tier(adapter):
    handlers, principal, _, _ = adapter
    principal.scopes = ["ask"]
    with pytest.raises(AgentToolError, match="缺少「读取」权限") as refused:
        await handlers["read_reference"](ctx=None, ref=_gel("gask-a", "el-a"))
    assert refused.value.code == "scope_missing"


@pytest.mark.anyio
async def test_get_ask_forwards_current_owner_and_allowlist(adapter):
    handlers, principal, _, service = adapter
    calls = []
    service.get_job = lambda job_id, **kw: (calls.append((job_id, kw)), job())[1]
    await handlers["get_ask"]("gask-a", None)
    principal.notebook_ids = ["nb-b"]
    await handlers["get_ask"]("gask-a", None)
    assert calls == [
        ("gask-a", {"user_id": "owner-a", "allowed_notebook_ids": BOTH}),
        ("gask-a", {"user_id": "owner-a", "allowed_notebook_ids": ["nb-b"]}),
    ]


@pytest.mark.anyio
async def test_a_global_job_replays_to_an_ask_only_token_because_it_never_reads_memory(
    adapter,
):
    """Global jobs carry no Memory gate on replay (the single-notebook path
    refuses a Memory-open job to a token without ``read``). That rests on one
    fact pinned here: a global run is a peer-mode (subjectless) run, and the
    one door every Ask Memory read goes through, ``AskService._memory_hits``,
    answers empty inside it. If that ever changes, a global replay needs the
    same ``memory_access`` gate as ``get_ask`` on a notebook job."""
    from app.services.ask_service import AskService
    from tests.test_peer_mode_ask_steps import _RecordingMemory, _peer_scope

    retriever = _RecordingMemory()
    with _peer_scope():
        assert AskService._memory_hits(
            SimpleNamespace(memory_retriever=retriever), "owner-a", "nb-a", "q"
        ) == []
    assert retriever.calls == []

    handlers, principal, _, service = adapter
    assert principal.scopes == ["ask"]  # no ``read``: Memory is closed to it
    service.get_job = lambda job_id, **kw: job(answer="跨库答案")
    service.replay = lambda payload, **kw: job(answer="跨库答案")
    read = await handlers["get_ask"]("gask-a", None)
    retried = await handlers["ask"](
        "问题", None, notebooks=BOTH, mode="chunk", client_request_id="g-key",
    )
    assert read["answer"] == retried["answer"] == "跨库答案"


@pytest.mark.anyio
async def test_get_continues_coverage_independently_and_rejects_negative_offset(adapter):
    handlers, _, _, service = adapter
    saved = job(answer="完整答案")
    saved.degraded_notebook_ids = [f"nb-{index}" for index in range(32)]
    calls = []
    service.get_job = lambda *_args, **_kw: calls.append(True) or saved
    first = await handlers["get_ask"]("gask-a", None)
    last = await handlers["get_ask"]("gask-a", None, coverage_offset=first["next_coverage_offset"])
    assert first["answer"] == last["answer"] == "完整答案"
    assert last["coverage"]["degraded_notebook_ids"] == saved.degraded_notebook_ids[RESULT_LIMIT:]
    assert last["next_coverage_offset"] is None
    with pytest.raises(AgentToolError, match="分页位置不能小于零") as refused:
        await handlers["get_ask"]("gask-a", None, coverage_offset=-1)
    assert refused.value.code == "invalid_argument"
    assert len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("error", [RuntimeError, ValueError, PermissionError])
async def test_unexpected_service_errors_never_echo_private_details(adapter, error):
    handlers, _, _, service = adapter

    def fail(*_args, **_kwargs):
        raise error("private-sentinel SQL credential path")

    service.get_job = fail
    with pytest.raises(AgentToolError, match="请稍后重试") as caught:
        await handlers["get_ask"]("gask-a", None)
    assert caught.value.code == "internal"
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
        page = _page(saved, answer_offset, citation_offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        assert page["job_id"] == "gask-a"
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
    # Every citation carries its read_reference handle (kind ``gel``).
    assert [row["ref"] for row in collected_refs] == [
        _gel("gask-a", row["element_id"]) for row in refs
    ]


@pytest.mark.anyio
async def test_citation_original_text_pages_and_knowledge_only_scope(adapter):
    handlers, principal, _, service = adapter
    principal.scopes = ["read"]
    full_text = "原文😀\n" * 1700
    calls = []

    def cited(job_id, element_id, **kw):
        calls.append((job_id, element_id, kw))
        return {"id": element_id, "source_id": "source-a", "text": full_text,
                "element_type": "text", "location_label": "第三页"}

    service.cited_element = cited
    # The drill-down reads the job first (to refuse a flagged citation); this
    # job cites nothing flagged, so every page reaches ``cited_element``.
    service.get_job = lambda *_args, **_kw: job()
    offset, pages = 0, []
    while True:
        page = await handlers["read_reference"](_gel("gask-a", "element-a"), None, offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= TOTAL_TEXT_LIMIT
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == full_text
    assert page["kind"] == "gel" and page["job_id"] == "gask-a"
    assert all(call == ("gask-a", "element-a", {
        "user_id": "owner-a", "allowed_notebook_ids": BOTH,
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

    async def run(ctx, work, *, label, on_cancel=None):
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
async def test_official_client_global_tools_and_their_citation_refs(mcp_env, monkeypatch):
    notebook_id = mcp_env["notebook"].id
    saved = job(answer="跨库答案", citations=[_citation(element_id="element-a")])
    saved.resolved_notebook_ids = [notebook_id]
    calls = []

    def start(payload, **kwargs):
        calls.append(kwargs)
        assert kwargs["authority_check"]() == [notebook_id]
        return saved

    service = SimpleNamespace(
        start=start, get_job=lambda *_args, **_kw: saved,
        cited_element=lambda *_args, **_kw: {
            "id": "element-a", "source_id": "source-a", "text": "引用原文",
        },
    )
    monkeypatch.setattr(module, "global_ask_service", lambda: service)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        # A gconv- conversation routes to global Ask and inherits its scope.
        started = _payload(await client.call("ask", {
            "question": "跨库结论是什么？", "conversation_id": "gconv-a", "mode": "chunk",
        }))
        assert started["job_id"] == "gask-a"
        result = _payload(await client.call("get_ask", {"job_id": "gask-a", "coverage_offset": 1}))
        assert result["answer"] == "跨库答案"
        assert result["coverage"]["offset"] == 1 and result["next_coverage_offset"] is None
        ref = result["citations"][0]["ref"]
        element = _payload(await client.call("read_reference", {"ref": ref}))
        assert element["text"] == "引用原文"
        assert element["job_id"] == "gask-a" and element["element_id"] == "element-a"
        assert (await client.call("cancel_global_ask", {"job_id": "gask-a"})).isError
    assert calls[0]["allowed_notebook_ids"] == [notebook_id]


# --- PR-D:终态引用核对部分失败 ----------------------------------------------
#
# 回答照常交付;摘要进 ``coverage.citation_check``,失败的引用带 ``verification``;
# 顶层键数仍是 20(``OUTPUT_MAPPING_LIMIT``),不新增锚点输出;失败引用的下钻关闭。

# The page's 18 always-present top-level keys, plus the budget's own
# ``truncation`` stats; ``intent`` and ``anchors`` join only when they carry
# something, so a page never exceeds ``OUTPUT_MAPPING_LIMIT`` (20).
_PAGE_KEYS = {
    "job_id", "conversation_id", "status", "mode", "answer_id", "answer",
    "next_answer_offset", "citations", "next_citation_offset", "total_citations",
    "grounded", "coverage", "next_coverage_offset", "trace", "scope",
    "error", "completeness_notice", "content_is_untrusted_evidence",
}
_COVERAGE_KEYS = {
    "resolved", "searched", "cited", "skipped", "degraded", "offset",
    "skipped_notebooks", "degraded_notebook_ids",
}
_CHECK = {
    "outcome": "partial", "checked": 2, "failed": 1,
    "changed": 1, "source_gone": 0, "unverifiable": 0,
}


def _engine_job(*, check=None, flagged="changed"):
    """A job answered by the shared engine, one citation flagged, one clean."""
    value = job()
    value.response = None
    value.answer = AskResponse.model_validate({
        "answer_id": "answer-a", "conclusion": "结论", "answer": "答案 [1][2]",
        "grounded": False,
        "citations": [
            _citation(element_id="e-flagged", **({"verification": flagged} if flagged else {})),
            _citation(element_id="e-clean", source_id="s-b"),
        ],
        **({"citation_check": check} if check else {}),
    })
    return value


def test_a_partly_failed_answer_carries_the_summary_under_coverage():
    page = _page(_engine_job(check=_CHECK))

    assert page["answer"] == "答案 [1][2]"
    assert page["coverage"]["citation_check"] == _CHECK
    assert [row.get("verification") for row in page["citations"]] == ["changed", None]
    assert set(page) == _PAGE_KEYS | {"truncation"}
    assert len(_PAGE_KEYS) + 2 == OUTPUT_MAPPING_LIMIT == 20
    assert page["truncation"]["truncated"] is False
    # No anchor output on this surface.
    assert "anchors" not in page


def test_a_clean_answer_page_is_unchanged():
    page = _page(_engine_job(flagged=None))

    assert set(page) == _PAGE_KEYS | {"truncation"}
    assert set(page["coverage"]) == _COVERAGE_KEYS
    assert "verification" not in json.dumps(page, ensure_ascii=False)
    # The legacy shape predates the check and reads back the same way.
    legacy = _page(job(answer="旧形状", citations=[_citation()]))
    assert set(legacy["coverage"]) == _COVERAGE_KEYS


@pytest.mark.anyio
async def test_a_flagged_citation_cannot_be_opened_and_a_clean_one_still_can(adapter):
    """The gate lives in ``GlobalAskService.cited_element`` (one job read, one
    exit); the tool only translates the service's refusal into its error."""
    from app.models.global_ask import FLAGGED_CITATION_MESSAGE, global_citation_flagged
    from app.services.global_ask import GlobalAskError

    handlers, principal, _, service = adapter
    principal.scopes = ["read"]
    saved = _engine_job(check=_CHECK)
    calls, opened = [], []

    def cited(job_id, element_id, **kw):
        calls.append((job_id, element_id, kw))
        if global_citation_flagged(saved, element_id):
            raise GlobalAskError(404, FLAGGED_CITATION_MESSAGE)
        opened.append(element_id)
        return {"id": element_id, "source_id": "s-b", "text": "原文",
                "element_type": "text", "location_label": "第一页"}

    service.cited_element = cited
    with pytest.raises(AgentToolError, match="未通过核对") as refused:
        await handlers["read_reference"](_gel("gask-a", "e-flagged"), None)
    assert refused.value.code == "not_found"
    assert refused.value.message == FLAGGED_CITATION_MESSAGE
    assert opened == []

    page = await handlers["read_reference"](_gel("gask-a", "e-clean"), None)
    assert page["text"] == "原文" and opened == ["e-clean"]
    # One service call per open, under the same owner and allowlist.
    assert [call[2] for call in calls] == [
        {"user_id": "owner-a", "allowed_notebook_ids": BOTH}
    ] * 2


def test_an_element_flagged_only_on_its_anchor_is_flagged_for_the_drill_down():
    """Reasoning answers display anchors first; a marker there closes the element."""
    from app.models.global_ask import global_citation_flagged

    saved = _engine_job(flagged=None)
    saved.answer.anchors = [AnswerAnchor(
        key="k1", object_id="o", object_type="passage", label="标签",
        element_id="e-clean", verification="source_gone",
    )]
    assert global_citation_flagged(saved, "e-clean") is True
    assert global_citation_flagged(saved, "e-flagged") is False
    assert global_citation_flagged(job(citations=[_citation()]), "e-a") is False


@pytest.mark.anyio
async def test_global_clarification_answers_that_do_not_fit_are_invalid_argument(adapter):
    """``validate_confirmed_intent``'s refusals (a required row left
    unanswered; a handle answered under a different question) are the Agent's
    to fix on the global path too -- never ``internal``."""
    handlers, _, _, service = adapter
    from app.models.ask import QueryIntentAmbiguity, QueryIntentContract

    def preview_intent(payload, **kwargs):
        return QueryIntentContract(
            objective=payload.question, resolved_question=payload.question,
            needs_clarification=True,
            ambiguities=[QueryIntentAmbiguity(id="a1", question="具体指哪一个项目？")],
        )

    service.preview_intent = preview_intent
    service.start = lambda payload, **kwargs: pytest.fail("must not start a job")
    paused = await handlers["ask"]("它的结论是什么", None, notebooks=BOTH)
    token = paused["intent_token"]
    with pytest.raises(AgentToolError) as unanswered:
        await handlers["ask"](
            "它的结论是什么", None, notebooks=BOTH,
            intent={"intent_token": token, "answers": []},
        )
    assert unanswered.value.code == "invalid_argument"
    with pytest.raises(AgentToolError) as mismatched:
        await handlers["ask"](
            "另一个完全不同的问题", None, notebooks=BOTH,
            intent={"intent_token": token, "answers": [{"id": "a1", "answer": "甲"}]},
        )
    assert mismatched.value.code == "invalid_argument"


@pytest.mark.anyio
async def test_a_browser_global_job_is_not_found_through_get_ask(adapter):
    handlers, _, _, service = adapter
    browser = job()
    browser.submitted_via = "web"
    service.get_job = lambda *_a, **_k: browser
    with pytest.raises(AgentToolError) as refused:
        await handlers["get_ask"]("gask-a", None)
    assert refused.value.code == "not_found"


def test_a_global_409_maps_by_its_reason_code_not_its_copy():
    """Only the coded key-reuse 409 is ``invalid_argument``; the same words
    without the code (or any other 409) stay ``busy``. The real replay path
    raising the code is pinned in tests/test_global_ask_wait.py."""
    from app.services.global_ask import REQUEST_KEY_REUSED, GlobalAskError

    coded = module.global_ask_tool_error(
        GlobalAskError(409, "请求标识已用于其他问题，请重新提交。", REQUEST_KEY_REUSED)
    )
    assert coded.code == "invalid_argument"
    assert "另一个问题" in coded.message
    uncoded = module.global_ask_tool_error(
        GlobalAskError(409, "请求标识已用于其他问题，请重新提交。")
    )
    assert uncoded.code == "busy"
    still_busy = module.global_ask_tool_error(
        GlobalAskError(409, "这段对话仍在回答，请等待完成或停止后重试。")
    )
    assert still_busy.code == "busy"


@pytest.mark.anyio
async def test_an_abandoned_global_wait_propagates_untouched_and_logs_nothing(
    adapter, caplog,
):
    """A wait the cancelled call let go of is not a failure of the run: it
    leaves ``_run_global`` as the very ``AskWaitAbandoned`` -- which only the
    tool's outer handler turns into its ``unavailable`` "等待已取消" copy --
    never mapped to ``internal``, and no "global ask via MCP failed" warning."""
    import logging

    handlers, _, _, service = adapter
    running = job()
    running.status = "running"
    service.start = lambda payload, **kwargs: running

    def wait(job_id, **kwargs):
        raise ask_module.AskWaitAbandoned()

    service.wait = wait
    with caplog.at_level(logging.WARNING):
        with pytest.raises(AgentToolError) as abandoned:
            await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert abandoned.value.code == "unavailable"
    assert "等待已取消" in abandoned.value.message
    assert "global ask via MCP failed" not in caplog.text


@pytest.mark.anyio
async def test_a_stalled_foreign_global_job_is_unavailable_not_internal(adapter, caplog):
    """``GlobalAskService.wait`` gives up on another process's job that made
    no progress (``AskExecutorGone``): the Agent gets the same
    ``unavailable`` copy as the notebook path, with no failure warning."""
    import logging

    from app.domain.cancellation import AskExecutorGone

    handlers, _, _, service = adapter
    running = job()
    running.status = "running"
    service.start = lambda payload, **kwargs: running

    def wait(job_id, **kwargs):
        raise AskExecutorGone()

    service.wait = wait
    with caplog.at_level(logging.WARNING):
        with pytest.raises(AgentToolError) as gone:
            await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert gone.value.code == "unavailable"
    assert "get_ask" in gone.value.message
    assert "global ask via MCP failed" not in caplog.text


def _delivery_rig(adapter, monkeypatch, change_during_wait):
    """A global ask whose job finishes only after ``change_during_wait`` ran
    (inside ``wait``); ``get_job`` applies the real service's allowlist rule
    (``GlobalAskService._check``: a resolved library outside the allowlist is
    a 404). Returns the handlers, the ledger and what ``get_job`` was asked."""
    from app.services.global_ask import GlobalAskError

    handlers, principal, repo, service = adapter
    running = job()
    running.status = "running"
    running.resolved_notebook_ids = list(BOTH)
    finished = job(answer="跨库答案")
    finished.resolved_notebook_ids = list(BOTH)
    recorded: list = []
    reads: list = []
    monkeypatch.setattr(ask_module, "_record_agent_call", lambda *args: recorded.append(args))
    service.start = lambda payload, **kwargs: running

    def wait(job_id, **kwargs):
        change_during_wait(principal, repo)
        return finished

    def get_job(job_id, *, user_id, allowed_notebook_ids=None):
        reads.append(list(allowed_notebook_ids or []))
        if not set(finished.resolved_notebook_ids) <= set(allowed_notebook_ids or []):
            raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")
        return finished

    service.wait = wait
    service.get_job = get_job
    return handlers, recorded, reads


@pytest.mark.anyio
@pytest.mark.parametrize("change, code", [
    ("revoked", "token_inactive"),
    ("de_tiered", "scope_missing"),
    ("allowlist_shrunk", "not_found"),
])
async def test_a_global_answer_is_delivered_under_the_token_as_it_is_now(
    adapter, monkeypatch, change, code,
):
    """The wait may take minutes: a token revoked, stripped of the ``ask``
    tier, or no longer allowlisted for one of the job's libraries by the time
    the job finishes gets get_ask's refusal -- no answer text, no ledger
    entry -- while the job itself stays answered for a later get_ask."""

    def during_wait(principal, repo):
        if change == "revoked":
            repo.refresh_agent_principal = lambda _token_id: None
        elif change == "de_tiered":
            principal.scopes = ["read"]
        else:
            principal.notebook_ids = ["nb-a"]

    handlers, recorded, _ = _delivery_rig(adapter, monkeypatch, during_wait)
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert refused.value.code == code
    assert "跨库答案" not in refused.value.message
    assert recorded == [], "a refused delivery must not be booked"


@pytest.mark.anyio
async def test_a_still_authorised_global_caller_receives_the_answer(adapter, monkeypatch):
    handlers, recorded, reads = _delivery_rig(
        adapter, monkeypatch, lambda principal, repo: None
    )
    page = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert page["answer"] == "跨库答案"
    assert reads == [BOTH], "the delivered job is re-read under the live allowlist"
    assert [args[2] for args in recorded] == BOTH


@pytest.mark.anyio
async def test_a_global_follow_failure_is_unavailable_with_get_ask_guidance(adapter, caplog):
    import logging

    from app.domain.cancellation import AskFollowFailed

    handlers, _, _, service = adapter
    running = job()
    running.status = "running"
    service.start = lambda payload, **kwargs: running

    def wait(job_id, **kwargs):
        raise AskFollowFailed()

    service.wait = wait
    with caplog.at_level(logging.WARNING):
        with pytest.raises(AgentToolError) as failed:
            await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert failed.value.code == "unavailable"
    assert "get_ask" in failed.value.message
    assert "global ask via MCP failed" not in caplog.text


@pytest.mark.anyio
async def test_a_browser_job_returned_by_start_itself_is_not_found(adapter, monkeypatch):
    """``start`` replays (or recovers an insert race to) the job already under
    the key -- which a browser request may have created after the replay
    probe. That job is refused like ``get_ask`` refuses it: ``not_found``,
    before any ledger entry, wait or content."""
    handlers, _, _, service = adapter
    browser = job(answer="浏览器的回答")
    browser.submitted_via = "web"
    browser.status = "running"
    recorded: list = []
    waited: list = []
    monkeypatch.setattr(ask_module, "_record_agent_call", lambda *args: recorded.append(args))
    service.start = lambda payload, **kwargs: browser
    service.wait = lambda job_id, **kwargs: waited.append(job_id)
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"](
            "问题", None, notebooks=BOTH, mode="chunk", client_request_id="raced-key",
        )
    assert refused.value.code == "not_found"
    assert recorded == [] and waited == []
    assert "浏览器的回答" not in refused.value.message


@pytest.mark.anyio
async def test_a_global_retry_differing_only_in_whitespace_is_the_same_job(adapter):
    """The MCP global path spells the question as the notebook store does, so
    ``"  Q?\\n"`` and ``"Q?"`` under one key are one submission: the retry is
    replayed (the real request comparison, ``_same_request``) instead of
    being refused as a key reused for another question."""
    from app.services.global_ask import (
        REQUEST_KEY_REUSED, GlobalAskError, GlobalAskService,
    )

    handlers, _, _, service = adapter
    stored: dict = {}
    started: list = []

    def start(payload, **kwargs):
        started.append(payload.question)
        stored[payload.client_request_id] = payload.model_dump_json()
        return job()

    def replay(payload, **kwargs):
        previous = stored.get(payload.client_request_id)
        if previous is None:
            return None
        if not GlobalAskService._same_request(previous, payload.model_dump_json()):
            raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。", REQUEST_KEY_REUSED)
        return job()

    service.start, service.replay = start, replay
    first = await handlers["ask"](
        "  Q?\n", None, notebooks=BOTH, mode="chunk", client_request_id="ws-key",
    )
    again = await handlers["ask"](
        "Q?", None, notebooks=BOTH, mode="chunk", client_request_id="ws-key",
    )
    assert first["job_id"] == again["job_id"] == "gask-a"
    assert started == ["Q?"], "the retry must replay, not start a second job"
