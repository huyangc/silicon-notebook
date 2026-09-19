"""Global MCP boundary and resumable transport contracts (no ambient services)."""
from types import SimpleNamespace
import json

import pytest

from app.api.mcp_tools import global_ask as module
from app.api.mcp_tools import session as session_module
from app.api.mcp_tools._shared import TOTAL_TEXT_LIMIT
from tests.test_memory_mcp import OfficialMcpClient, _payload, mcp_env


class Capture:
    def __init__(self):
        self.handlers = {}

    def tool(self, *, description):
        def register(handler):
            self.handlers[handler.__name__] = handler
            return handler
        return register


def job(*, answer="", citations=None):
    return {
        "job_id": "job-a", "conversation_id": "conversation-a", "status": "done",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": ["nb-a"], "searched_notebook_ids": ["nb-a"],
        "cited_notebook_ids": ["nb-a"], "error": None,
        "response": {"answer_id": "answer-a", "answer": answer, "grounded": True,
                     "citations": citations or []},
    }


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
    refs = [{
        "notebook_id": f"nb-{i}", "source_id": f"source-{i}",
        "element_id": f"element-{i}", "quoted_span": "引用内容" * 500,
        "source_file_name": "很长的标题" * 100,
    } for i in range(43)]
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
    saved["resolved_notebook_ids"] = [notebook_id]
    calls = []

    def start(payload, **kwargs):
        calls.append(kwargs)
        assert kwargs["authority_check"]() == [notebook_id]
        return saved

    service = SimpleNamespace(
        start=start, get_job=lambda *_args, **_kw: saved,
        cancel=lambda *_args, **_kw: {**saved, "status": "cancelled"},
        cited_element=lambda *_args, **_kw: {
            "id": "element-a", "source_id": "source-a", "text": "引用原文",
        },
    )
    monkeypatch.setattr(module, "global_ask_service", lambda: service)
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        started = _payload(await client.call("ask_global", {"question": "跨库结论是什么？"}))
        assert started["job_id"] == "job-a"
        result = _payload(await client.call("get_global_ask", {"job_id": "job-a"}))
        assert result["answer"] == "跨库答案"
        element = _payload(await client.call("get_global_cited_element", {
            "job_id": "job-a", "element_id": "element-a",
        }))
        assert element["text"] == "引用原文"
        cancelled = _payload(await client.call("cancel_global_ask", {"job_id": "job-a"}))
        assert cancelled["status"] == "cancelled"
    assert calls[0]["allowed_notebook_ids"] == [notebook_id]
