from __future__ import annotations

import asyncio
import base64
import json
import pathlib
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.api.deps import (
    identity_repository,
    mcp_memory_repository,
    notebook_catalog_repository,
    notebook_sharing_repository,
    repository,
)
from app.api.mcp_server import PUBLIC_TOOLS as mcp_server_public_tools
from app.core.config import get_settings
from app.core.request_context import reset_request_user, set_request_user
from app.api.source_routes import document_capacity_message
from app.models.schemas import MemoryHit, NotebookCreate
from tests.model_testkit import bind_chat_client


# DERIVED from the manifest, deliberately not a third hand-copied list.
#
# `mcp_server.PUBLIC_TOOLS` derives from the same default frozen combined
# catalog used by live discovery/onboarding. `CORE_TOOLS` names the exact
# built-in prefix when a test reasons about registrar-owned handlers.
PUBLIC_TOOLS = set(mcp_server_public_tools)
MCP_OUTPUT_BUDGET = 12_000


def _payload(result):
    assert not result.isError, result
    if result.structuredContent is not None:
        return result.structuredContent
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


def _assert_budgeted(payload: dict) -> None:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert len(serialized.encode("utf-8")) <= MCP_OUTPUT_BUDGET
    assert payload["truncation"]["budget_chars"] == MCP_OUTPUT_BUDGET
    assert isinstance(payload["truncation"]["truncated"], bool)
    assert "private-budget-sentinel" not in json.dumps(
        payload["truncation"], ensure_ascii=False
    )


def _json_chars(value) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))


def _omitted_total(payload: dict) -> int:
    metadata = payload["truncation"]
    return sum(
        int(metadata[key])
        for key in (
            "omitted_items",
            "omitted_map_entries",
            "omitted_characters",
            "omitted_fields",
        )
    )


async def _wait_for_ready(app) -> None:
    """Wait for the real background lifespan warm-up before MCP traffic."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        deadline = asyncio.get_running_loop().time() + 5
        snapshot = None
        while asyncio.get_running_loop().time() < deadline:
            snapshot = (await client.get("/api/ready")).json()
            if snapshot["ready"]:
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"service did not become ready: {snapshot}")


class OfficialMcpClient:
    def __init__(self, app, token: str, *, manage_lifespan: bool = True):
        self.app = app
        self.token = token
        self.manage_lifespan = manage_lifespan
        self.stack = AsyncExitStack()
        self.session = None
        self.http = None
        self.mcp_session_id = ""

    async def __aenter__(self):
        if self.manage_lifespan:
            await self.stack.enter_async_context(
                self.app.router.lifespan_context(self.app)
            )
        await _wait_for_ready(self.app)
        async def capture_session_id(response: httpx.Response):
            if value := response.headers.get("mcp-session-id"):
                self.mcp_session_id = value

        http = await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://127.0.0.1",
                headers={"Authorization": f"Bearer {self.token}"},
                follow_redirects=True,
                event_hooks={"response": [capture_session_id]},
            )
        )
        self.http = http
        read, write, _ = await self.stack.enter_async_context(
            streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                # ASGITransport executes the server in this same task.  A
                # protocol DELETE would therefore cancel the test's own
                # AnyIO scope; real network clients run in a separate server
                # task/process and use the default terminate_on_close=True.
                terminate_on_close=False,
            )
        )
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc_info):
        await self.stack.aclose()

    async def call(
        self, name: str, arguments: dict | None = None, *, progress_callback=None
    ):
        assert self.session is not None
        # Passing a progress_callback is what makes the SDK put a
        # `progressToken` in the request's `_meta`; without one the server's
        # `Context.report_progress` short-circuits and nothing is sent.
        return await self.session.call_tool(
            name, arguments or {}, progress_callback=progress_callback
        )


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mcp.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    # MinerU must be cleared EXPLICITLY (repo red line): pydantic-settings would
    # otherwise read the repository-root .env and give these tests a real cloud
    # token, at which point add_source_url's probe path could reach the network.
    # Cleared here rather than per-test because Settings is captured when the
    # fixture builds the app — a later monkeypatch would not reach the runtime.
    monkeypatch.setenv("MINERU_MODE", "off")
    monkeypatch.setenv("MINERU_API_URL", "")
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    # Startup declares the public deployment as HTTPS and pins the strict
    # (fail-closed) policy so the runtime transport still rejects a remote
    # client that reaches ASGI over plain HTTP. The product default is now
    # open (MCP_REQUIRE_HTTPS unset); these tests exercise the strict path.
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://memory.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
    get_settings.cache_clear()
    repository.cache_clear()

    from app.main import create_app

    app = create_app()
    identity = identity_repository()
    catalog = notebook_catalog_repository()
    sharing = notebook_sharing_repository()
    service = mcp_memory_repository()
    alice = identity.create_user("a00128001", "pw")
    bob = identity.create_user("b00128002", "pw")
    marker = set_request_user(alice)
    try:
        notebook = catalog.create_notebook(
            NotebookCreate(name="MCP notebook")
        )
        other = catalog.create_notebook(
            NotebookCreate(name="Other notebook")
        )
    finally:
        reset_request_user(marker)
    profile_a = service.create_agent_profile(alice.id, "Claude Code", "")
    profile_b = service.create_agent_profile(alice.id, "Codex", "")
    scopes = [
        "knowledge:read",
        "memory:read",
        "memory:read_candidates",
        "memory:propose",
        "ask:execute",
    ]
    token_a = service.issue_agent_token(
        alice.id, profile_a.id, scopes, notebook.id, [notebook.id], None
    )
    token_b = service.issue_agent_token(
        alice.id, profile_b.id, scopes, notebook.id, [notebook.id], None
    )
    restricted = service.issue_agent_token(
        alice.id, profile_b.id, ["memory:read"], notebook.id, [notebook.id], None
    )
    bob_profile = service.create_agent_profile(bob.id, "Foreign", "")
    sharing.add_member(notebook.id, bob.id)
    bob_token = service.issue_agent_token(
        bob.id, bob_profile.id, scopes, notebook.id, [notebook.id], None
    )
    return {
        "app": app,
        "sharing": sharing,
        "service": service,
        "alice": alice,
        "bob": bob,
        "notebook": notebook,
        "other": other,
        "profile_a": profile_a,
        "profile_b": profile_b,
        "bob_profile": bob_profile,
        "token_a": token_a,
        "token_b": token_b,
        "restricted": restricted,
        "bob_token": bob_token,
    }


@pytest.mark.anyio
async def test_official_client_exposes_exact_public_tool_contract(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        listed = await client.session.list_tools()
        notebooks = _payload(await client.call("list_notebooks"))
        assert [item["notebook_id"] for item in notebooks["items"]] == [
            mcp_env["notebook"].id
        ]
        assert (await client.call(
            "select_notebook", {"notebook_id": mcp_env["other"].id}
        )).isError
    assert {tool.name for tool in listed.tools} == PUBLIC_TOOLS


@pytest.mark.anyio
async def test_notebook_selection_is_required_and_session_scoped(mcp_env):
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as first:
            missing = await first.call("search_agent_memory", {"query": "anything"})
            assert missing.isError
            selected = _payload(await first.call(
                "select_notebook", {"notebook_id": mcp_env["notebook"].id}
            ))
            assert selected["notebook_id"] == mcp_env["notebook"].id
            assert not (await first.call(
                "search_agent_memory", {"query": "anything"}
            )).isError

        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as second:
            assert (await second.call(
                "search_agent_memory", {"query": "anything"}
            )).isError


@pytest.mark.anyio
async def test_candidate_is_agent_recallable_across_owner_profiles_but_not_notebook_context(mcp_env):
    notebook_id = mcp_env["notebook"].id
    content = "Use the distinctive kelvin guard-ring procedure before extraction."
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as creator:
            _payload(await creator.call("select_notebook", {"notebook_id": notebook_id}))
            created = _payload(await creator.call("propose_memory", {
                "title": "Kelvin guard ring",
                "content_md": content,
                "tags": ["layout"],
                "reason": "Worked in the previous task",
                "task_context": {"task": "extraction"},
                "evidence_refs": [],
                "client_request_id": "request-1",
            }))
            repeated = _payload(await creator.call("propose_memory", {
                "title": "A changed retry title",
                "content_md": "A changed retry body",
                "tags": ["retry"],
                "reason": "Retry of the same logical request",
                "task_context": {"task": "different"},
                "evidence_refs": [{"source_id": "ignored-on-retry"}],
                "client_request_id": "request-1",
            }))
            memory_id = created["memory_id"]
            assert repeated["memory_id"] == memory_id
            memories = mcp_env["service"].list_memories(
                mcp_env["alice"].id,
                notebook_id=notebook_id,
                status="candidate",
            )
            assert [item.id for item in memories.items].count(memory_id) == 1
            assert created["status"] == "candidate"
            detail = _payload(await creator.call(
                "get_memory", {"memory_id": memory_id}
            ))
            assert detail["status"] == "candidate"
            notebook_hits = _payload(await creator.call(
                "search_notebook_context", {"query": "kelvin guard-ring"}
            ))
            assert memory_id not in {item.get("memory_id") for item in notebook_hits["items"]}

        async with OfficialMcpClient(
            app, mcp_env["token_b"].token, manage_lifespan=False
        ) as peer:
            _payload(await peer.call("select_notebook", {"notebook_id": notebook_id}))
            recalled = _payload(await peer.call(
                "search_agent_memory", {"query": "kelvin guard-ring"}
            ))
            hit = next(item for item in recalled["items"] if item["memory_id"] == memory_id)
            assert hit["status"] == "candidate"
            assert hit["unconfirmed"] is True
            assert hit["created_by_agent"] == "Claude Code"
            mcp_env["service"].confirm_memory(memory_id, mcp_env["alice"].id)
            formal = _payload(await peer.call(
                "search_notebook_context", {"query": "kelvin guard-ring"}
            ))
            assert memory_id in {item.get("memory_id") for item in formal["items"]}

        async with OfficialMcpClient(
            app, mcp_env["bob_token"].token, manage_lifespan=False
        ) as foreign:
            _payload(await foreign.call("select_notebook", {"notebook_id": notebook_id}))
            hits = _payload(await foreign.call(
                "search_agent_memory", {"query": "kelvin guard-ring"}
            ))
            assert memory_id not in {item["memory_id"] for item in hits["items"]}


@pytest.mark.anyio
async def test_access_is_rechecked_after_revoke_and_membership_loss(mcp_env):
    notebook_id = mcp_env["notebook"].id
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["bob_token"].token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            mcp_env["sharing"].remove_member(notebook_id, mcp_env["bob"].id)
            assert (await client.call("search_notebook_context", {"query": "x"})).isError

        mcp_env["service"].revoke_agent_token(
            mcp_env["alice"].id, mcp_env["token_a"].id
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {mcp_env['token_a'].token}"},
            follow_redirects=True,
        )
        async with http:
            response = await http.post(
                "/mcp",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        assert response.status_code == 401


@pytest.mark.anyio
async def test_same_profile_lower_scope_token_cannot_reuse_an_initialized_session(mcp_env):
    app = mcp_env["app"]
    notebook_id = mcp_env["notebook"].id
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_b"].token, manage_lifespan=False
        ) as client:
            _payload(await client.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            assert client.http is not None
            assert client.mcp_session_id
            response = await client.http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {mcp_env['restricted'].token}",
                    "Mcp-Session-Id": client.mcp_session_id,
                    "MCP-Protocol-Version": "2025-11-25",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "tools/call",
                    "params": {
                        "name": "search_notebook_context",
                        "arguments": {"query": "must not use the old scope snapshot"},
                    },
                },
            )
            assert response.status_code == 404


@pytest.mark.anyio
async def test_ask_tool_reuses_formal_ask_and_rejects_experimental_graph(mcp_env):
    from app.services.sqlite_repository import _now

    repo = repository()
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mcp-ask-source", mcp_env["notebook"].id, "Evidence", "markdown",
             "extracted", "parsed", "e.md", "", 0, "", "", "textbook",
             now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("mcp-ask-chunk", mcp_env["notebook"].id, "mcp-ask-source",
             "evidence exists in this notebook", "1", "[]", now),
        )

    class _AnswerClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            return '{"answer":"Evidence exists [k1].","grounded":true}'

    bind_chat_client(repo, "ask_answer", _AnswerClient())
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        graph = await client.call(
            "ask_notebook", {"question": "What is here?", "mode": "graph"}
        )
        assert graph.isError
        answer = _payload(await client.call(
            "ask_notebook", {"question": "What evidence exists?", "mode": "chunk"}
        ))
        assert answer["mode"] == "chunk"
        assert "answer" in answer
        # conversation_id round-trips (AskResponse.conversation_id is always
        # non-empty on success) and anchors/citations project the fields the
        # frontend needs to jump straight to the backing element/knowhow row.
        assert answer["conversation_id"]
        assert answer["anchors"]
        assert all(
            "source_id" in anchor and "element_id" in anchor
            for anchor in answer["anchors"]
        )
        assert answer["anchors"][0]["source_id"] == "mcp-ask-source"
        assert answer["citations"]
        assert all(
            citation["content_is_untrusted_evidence"] is True
            for citation in answer["citations"]
        )
        assert answer["citations"][0]["source_id"] == "mcp-ask-source"
        # Passing the returned conversation_id back continues the same
        # conversation instead of silently starting a new one.
        followup = _payload(await client.call(
            "ask_notebook",
            {
                "question": "What else is here?",
                "mode": "chunk",
                "conversation_id": answer["conversation_id"],
            },
        ))
        assert followup["conversation_id"] == answer["conversation_id"]
    assert "ask_answer" in repo._runtime.models._test_chat_calls


def _fake_notebook_summary(mcp_env):
    return mcp_env["notebook"].model_copy(update={"ask_available": True})


@pytest.mark.anyio
async def test_ask_notebook_projects_precise_anchor_and_citation_fields(
    mcp_env, monkeypatch
):
    """P1 mutation guard: exact-value assertions (not presence/upper-bound
    checks) for every field the ask_notebook projection adds. This is a small,
    controlled payload -- one anchor/citation with knowhow, one without, and
    over-limit quoted_span/source_file_name strings -- specifically so neither
    the citations sub-budget fit nor the global convergence loop touches
    anything, and the field_limits truncation math is exactly predictable.
    Mutations this must catch (verified by replay, see task report):
      1. deleting the anchors/citations knowhow projection blocks
      2. citation.element_id -> "" (dropping the real value)
      3. deleting the quoted_span/source_file_name field_limits entries
    """
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env))

    long_quoted_span = "证据原文内容片段" * 40  # 320 chars, over the 200-char limit
    long_file_name = "really-long-file-name-that-exceeds-the-limit-" * 8  # >300 chars

    monkeypatch.setattr(
        service,
        "ask",
        lambda *a, **k: SimpleNamespace(
            answer_id="ans-precise",
            answer="Answer [k1].",
            conclusion="Answer.",
            grounded=True,
            evidence_level="grounded",
            mode="chunk",
            conversation_id="conv-precise",
            anchors=[
                SimpleNamespace(
                    key="k1", object_id="obj-1", object_type="concept",
                    label="Anchor label", source_title="Source A",
                    location_label="1.1", source_id="anchor-source-1",
                    element_id="anchor-element-1", tier="personal",
                    provenance={},
                    knowhow=SimpleNamespace(table_id="tbl-1", row_id="row-1"),
                ),
                SimpleNamespace(
                    key="k2", object_id="obj-2", object_type="concept",
                    label="Anchor label 2", source_title="Source B",
                    location_label="1.2", source_id="anchor-source-2",
                    element_id="anchor-element-2", tier="personal",
                    provenance={}, knowhow=None,
                ),
            ],
            citations=[
                SimpleNamespace(
                    label="Citation A", source_id="citation-source-1",
                    element_id="citation-element-1", location_label="1.1",
                    quoted_span=long_quoted_span, source_file_name=long_file_name,
                    tier="personal", notebook_id="", memory_id="",
                    knowhow=SimpleNamespace(table_id="tbl-2", row_id="row-2"),
                ),
                SimpleNamespace(
                    label="Citation B", source_id="citation-source-2",
                    element_id="citation-element-2", location_label="1.2",
                    quoted_span="short span", source_file_name="short.md",
                    tier="personal", notebook_id="", memory_id="",
                    knowhow=None,
                ),
            ],
        ),
    )
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        answer = _payload(await client.call(
            "ask_notebook", {"question": "precise fields", "mode": "chunk"}
        ))

    anchors = answer["anchors"]
    assert anchors[0]["knowhow"] == {"table_id": "tbl-1", "row_id": "row-1"}
    assert "knowhow" not in anchors[1]

    citations = answer["citations"]
    assert citations[0]["source_id"] == "citation-source-1"
    assert citations[0]["element_id"] == "citation-element-1"
    assert citations[0]["knowhow"] == {"table_id": "tbl-2", "row_id": "row-2"}
    assert citations[1]["element_id"] == "citation-element-2"
    assert "knowhow" not in citations[1]
    # Empty notebook_id/memory_id are OMITTED, matching get_cited_element's rule
    # for the same field: a same-notebook citation restates the notebook the
    # Agent selected, and a non-Memory citation has no memory. Both fixtures
    # above carry "" for both fields, which is the ordinary case.
    for citation in citations:
        assert "notebook_id" not in citation
        assert "memory_id" not in citation

    # field_limits truncation is exact: value[:limit-1] + "…" (len == limit).
    assert citations[0]["quoted_span"] == long_quoted_span[:199] + "…"
    assert len(citations[0]["quoted_span"]) == 200
    assert citations[0]["source_file_name"] == long_file_name[:299] + "…"
    assert len(citations[0]["source_file_name"]) == 300
    # The untouched short citation proves the limit only clips, never pads.
    assert citations[1]["quoted_span"] == "short span"
    assert citations[1]["source_file_name"] == "short.md"


@pytest.mark.anyio
async def test_ask_notebook_preserves_answer_text_under_realistic_citation_load(
    mcp_env, monkeypatch
):
    """P0: with a realistic (non-adversarial) CJK payload -- an answer sized
    like a real synthesis, plus one citation per retrieved chunk at the
    chunk_mmr_k=16 default -- the shared _budget_response convergence loop
    must never shrink the answer text to make room for citations.

    Before citations_budget_chars, this broke in production: _serialized_size
    counts UTF-8 bytes (3 bytes/CJK char), but the convergence loop's
    _shrink_longest_string picks its victim by Python len() (characters).
    "answer" (field-limited to 6000 chars) was reliably the single longest
    string by character count and got halved repeatedly to make room for an
    unbounded citations list -- collapsing a ~900-character Chinese answer to
    ~112 characters in a real repro.
    """
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env))

    filler = "知识图谱检索增强生成技术在企业级应用中的落地实践与挑战分析"

    def zh_text(n: int) -> str:
        return (filler * (n // len(filler) + 1))[:n]

    answer_text = zh_text(1500)
    conclusion_text = zh_text(150)
    anchors = [
        SimpleNamespace(
            key=f"k{i + 1}", object_id=f"obj-{i}", object_type="concept",
            label=zh_text(20), source_title=zh_text(15),
            location_label=f"2.{i}", source_id=f"source-{i}",
            element_id=f"element-{i}", tier="personal", provenance={},
            knowhow=None,
        )
        for i in range(8)
    ]
    citations = [
        SimpleNamespace(
            label=zh_text(15) + f" · {i}", source_id=f"source-{i}",
            element_id=f"element-{i}", location_label=f"2.{i}",
            quoted_span=zh_text(200), source_file_name=f"doc-{i}.md",
            tier="personal", notebook_id="", memory_id="", knowhow=None,
        )
        for i in range(16)
    ]
    monkeypatch.setattr(
        service,
        "ask",
        lambda *a, **k: SimpleNamespace(
            answer_id="ans-realistic", answer=answer_text,
            conclusion=conclusion_text, grounded=True,
            evidence_level="grounded", mode="chunk",
            conversation_id="conv-realistic",
            anchors=anchors, citations=citations,
        ),
    )
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        answer = _payload(await client.call(
            "ask_notebook", {"question": "realistic load", "mode": "chunk"}
        ))
    # The core guardrail: answer survives byte-for-byte, and the response
    # still fits the public MCP JSON budget.
    assert answer["answer"] == answer_text
    _assert_budgeted(answer)


@pytest.mark.anyio
async def test_ask_notebook_rejects_conversation_id_over_max_length(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        rejected = await client.call(
            "ask_notebook",
            {
                "question": "anything",
                "mode": "chunk",
                "conversation_id": "c" * 201,
            },
        )
        assert rejected.isError
        assert "too long" in rejected.content[0].text


@pytest.mark.anyio
async def test_ask_notebook_rejects_a_question_over_max_length(mcp_env):
    """MCP 这个 ask 入口与 /ask、/ask/stream 受同一条长度闸约束。

    公开分享页把每轮 question 原样发给匿名访客,所以「原样返回」只有在**每个**写入
    侧都拒收超长问题时才是有界的——长期 token 的 Agent 客户端也是写入侧。这是一次
    刻意的行为变化(此前 MCP 可提交任意长度问题),已在 docs 登记。

    检查刻意写在工具体内、而不是交给 `AskRequest` 的校验器:后者抛出的
    ValidationError 对 Agent 是一坨不可读的 model dump,前者说得清该怎么办——与
    `conversation_id too long` 同一条既有惯例。
    """
    from app.models.ask import ASK_QUESTION_MAX_CHARS

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        rejected = await client.call(
            "ask_notebook",
            {"question": "问" * (ASK_QUESTION_MAX_CHARS + 1), "mode": "chunk"},
        )
        assert rejected.isError
        text = rejected.content[0].text
        # 可操作:说清上限、当前长度和出路,不是一句裸 "invalid"。
        assert "question too long" in text
        assert str(ASK_QUESTION_MAX_CHARS) in text
        assert "source" in text

        # 恰好等于上限**不因长度**被拒(空转保护:恒拒的实现过不了)。空库仍会以
        # 「先添加来源」拒绝——那正好证明请求已经走过了长度闸、进到了可用性判定。
        at_cap = await client.call(
            "ask_notebook",
            {"question": "问" * ASK_QUESTION_MAX_CHARS, "mode": "chunk"},
        )
        assert at_cap.isError
        assert "too long" not in at_cap.content[0].text
        assert "来源" in at_cap.content[0].text


@pytest.mark.anyio
async def test_ask_notebook_filters_memory_citations_without_memory_read_scope(
    mcp_env, monkeypatch
):
    """Additional locked-in fix: chunk/reasoning ask unconditionally builds a
    Citation row for confirmed-Memory hits (Citation.memory_id non-empty). A
    token lacking memory:read must not see that row -- mirrors
    search_notebook_context's allow_memory gate exactly (same scope probe,
    same PermissionError-as-deny semantics), filtered at citation_rows
    construction so it never reaches the wire."""
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env))

    def fake_ask(*_a, **_k):
        return SimpleNamespace(
            answer_id="ans-memscope", answer="Answer [k1].", conclusion="Answer.",
            grounded=True, evidence_level="grounded", mode="chunk",
            conversation_id="conv-memscope", anchors=[],
            citations=[
                SimpleNamespace(
                    label="Memory hit", source_id="", element_id="",
                    location_label="", quoted_span="memory content",
                    source_file_name="", tier="personal", notebook_id="",
                    memory_id="mem-1", knowhow=None,
                ),
                SimpleNamespace(
                    label="Source hit", source_id="source-1", element_id="element-1",
                    location_label="1", quoted_span="source content",
                    source_file_name="doc.md", tier="personal", notebook_id="",
                    memory_id="", knowhow=None,
                ),
            ],
        )

    monkeypatch.setattr(service, "ask", fake_ask)

    ask_only_token = service.issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id, ["ask:execute"],
        notebook_id, [notebook_id], None,
    )
    full_scopes = [
        "knowledge:read", "memory:read", "memory:read_candidates",
        "memory:propose", "ask:execute",
    ]
    full_token = service.issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id, full_scopes,
        notebook_id, [notebook_id], None,
    )
    # The MCP session manager can only .run() once per app instance, so both
    # tokens share one outer lifespan (mirrors
    # test_notebook_selection_is_required_and_session_scoped's pattern).
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, ask_only_token.token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            without_scope = _payload(await client.call(
                "ask_notebook", {"question": "memory scoping", "mode": "chunk"}
            ))
        memory_ids = {c.get("memory_id") for c in without_scope["citations"]}
        assert "mem-1" not in memory_ids
        assert any(c["source_id"] == "source-1" for c in without_scope["citations"])
        # A scope-filtered row leaves NO trace in the truncation stats (this is
        # the search_notebook_context gate's behaviour, and it is the point):
        # a non-zero `omitted_items` here would tell a token without
        # memory:read exactly how many private Memory citations back this
        # answer -- the count is itself the disclosure.
        assert without_scope["truncation"]["truncated"] is False
        assert _omitted_total(without_scope) == 0

        async with OfficialMcpClient(
            app, full_token.token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            with_scope = _payload(await client.call(
                "ask_notebook", {"question": "memory scoping", "mode": "chunk"}
            ))
        assert any(c.get("memory_id") == "mem-1" for c in with_scope["citations"])


@pytest.mark.anyio
async def test_ask_notebook_memory_citation_count_is_not_recoverable_from_omitted(
    mcp_env, monkeypatch
):
    """The scope filter must run BEFORE the RESULT_LIMIT slice.

    With more citations than the limit, filtering after the slice leaks the
    exact number it is hiding through arithmetic: both tokens report the same
    `omitted_items` (computed from the unfiltered length), so the difference in
    returned row counts IS the private Memory count. The previous test's
    payload sits under the limit, where the bug is invisible.

    Here: 25 citations, 4 of them Memory-backed. A token with memory:read must
    see 20 rows; a token without must ALSO see 20 rows (there are 21 non-Memory
    citations, enough to fill the page), and the two omitted counts must differ
    exactly as the two visible populations differ — never in a way that lets
    the second token solve for 4.
    """
    from app.api.mcp_tools import memory_context

    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env))
    # Lift the citations sub-budget for this case only. At its real 1,800 chars
    # a 20-row page does not fit (~150 chars/row even with every field empty),
    # so the budget's own pre-fit would drop rows and fold ITS omissions into
    # the same counter — masking the ordering bug this test exists to catch.
    # The sub-budget has its own coverage in
    # test_ask_notebook_preserves_answer_text_under_realistic_citation_load.
    monkeypatch.setattr(memory_context, "CITATIONS_BUDGET_CHARS", 20_000)

    memory_positions = {2, 7, 13, 22}

    def fake_ask(*_a, **_k):
        return SimpleNamespace(
            answer_id="ans-leak", answer="Answer.", conclusion="Answer.",
            grounded=True, evidence_level="grounded", mode="chunk",
            conversation_id="conv-leak", anchors=[],
            citations=[
                SimpleNamespace(
                    label=f"C{index}",
                    source_id="" if index in memory_positions else f"s{index}",
                    element_id="" if index in memory_positions else f"e{index}",
                    location_label="1", quoted_span="q", source_file_name="d.md",
                    tier="personal", notebook_id="",
                    memory_id=f"mem-{index}" if index in memory_positions else "",
                    knowhow=None,
                )
                for index in range(25)
            ],
        )

    monkeypatch.setattr(service, "ask", fake_ask)
    full_token = service.issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id,
        ["knowledge:read", "memory:read", "ask:execute"],
        notebook_id, [notebook_id], None,
    )
    ask_only_token = service.issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id, ["ask:execute"],
        notebook_id, [notebook_id], None,
    )

    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, full_token.token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            with_scope = _payload(await client.call(
                "ask_notebook", {"question": "leak probe", "mode": "chunk"}
            ))
        async with OfficialMcpClient(
            app, ask_only_token.token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            without_scope = _payload(await client.call(
                "ask_notebook", {"question": "leak probe", "mode": "chunk"}
            ))

    assert len(with_scope["citations"]) == 20
    assert len(without_scope["citations"]) == 20, (
        "过滤发生在切片之前,21 条非 Memory 引用足以填满这一页"
    )
    assert not any(c.get("memory_id") for c in without_scope["citations"])
    # 25 - 20 = 5 with the scope, 21 - 20 = 1 without. The unscoped token sees a
    # SMALLER omitted count, so subtracting cannot recover 4; the pre-fix code
    # reported 5 to both, and 20 - 16 = 4 fell straight out.
    assert with_scope["truncation"]["omitted_items"] == 5
    assert without_scope["truncation"]["omitted_items"] == 1
    assert (
        len(without_scope["citations"])
        + without_scope["truncation"]["omitted_items"]
        == 21
    ), "无 memory:read 的 token 只该看到非 Memory 的那 21 条,总量不得泄露 25"


@pytest.mark.anyio
async def test_ask_notebook_rejects_empty_notebook(mcp_env):
    """PR#334 硬约束覆盖 MCP 这个 user-facing ask 入口:空库(无任何可检索证据)的
    ask_notebook 与 /ask、/ask/stream 一致被拒绝,不产生凭空回答(codex 第10轮 P2)。
    mcp_env['notebook'] 由 fixture 建空,未 seed 证据。"""
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        rejected = await client.call(
            "ask_notebook", {"question": "anything", "mode": "chunk"}
        )
        assert rejected.isError
        assert "来源" in rejected.content[0].text


@pytest.mark.anyio
async def test_each_data_tool_enforces_its_minimal_live_scope_and_output_budget(mcp_env):
    notebook_id = mcp_env["notebook"].id
    candidate = mcp_env["service"].create_memory_candidate(
        notebook_id,
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        "budget-request",
        "Budget marker",
        "budget-marker candidate-only",
        [],
        "test",
    )
    confirmed = mcp_env["service"].create_memory_candidate(
        notebook_id,
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        "budget-request-confirmed",
        "Budget marker confirmed",
        "budget-marker " + ("private-data " * 1000),
        [(f"tag-{index}-" + "sensitive-data-" * 4) for index in range(20)],
        "test",
    )
    mcp_env["service"].confirm_memory(confirmed.id, mcp_env["alice"].id)
    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["restricted"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        recalled = _payload(await client.call(
            "search_agent_memory", {"query": "budget-marker", "limit": 50}
        ))
        assert candidate.id not in {item["memory_id"] for item in recalled["items"]}
        confirmed_hit = next(
            item for item in recalled["items"] if item["memory_id"] == confirmed.id
        )
        assert len(confirmed_hit["content"]) <= 2_000
        assert len(recalled["items"]) <= 20
        detail = _payload(await client.call(
            "get_memory", {"memory_id": confirmed.id}
        ))
        assert isinstance(detail["tags"], list)
        assert all(len(tag) <= 200 for tag in detail["tags"])
        assert len(json.dumps(detail, ensure_ascii=False)) <= 12_000
        assert (await client.call(
            "search_notebook_context", {"query": "budget-marker"}
        )).isError
        assert (await client.call(
            "propose_memory",
            {
                "title": "No",
                "content_md": "No",
                "reason": "No",
                "task_context": {},
                "evidence_refs": [],
                "client_request_id": "no-scope",
            },
        )).isError
        assert (await client.call(
            "ask_notebook", {"question": "No scope", "mode": "chunk"}
        )).isError
        # Agentic Memory P3 (T3): the two newest data tools, each pinned by
        # their own scope like everything else above.
        assert (await client.call("get_notebook_profile", {})).isError
        assert (await client.call("add_observation", {
            "text": "No scope", "client_request_id": "budget-no-scope",
        })).isError


# The seven ORIGINAL (pre-knowhow) memory-surface tools this specific stress
# test exercises — kept as its own local constant (not the module-level
# PUBLIC_TOOLS, which now also carries the four knowhow-tables PR-2+3 Task 10
# tools) so this test's scope/name ("all seven") stays accurate regardless of
# what future tool surfaces get added to PUBLIC_TOOLS. Knowhow's own budget
# behavior gets its own smoke test below
# (test_knowhow_agent_tool_responses_respect_serialized_budget), mirroring
# this test's adversarial-data style at a smaller scale.
_MEMORY_TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "get_memory",
    "ask_notebook",
    "propose_memory",
}


@pytest.mark.anyio
async def test_all_seven_official_client_tool_responses_have_strict_serialized_budgets(
    mcp_env, monkeypatch
):
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    sentinel = "私密-private-budget-sentinel"
    huge_counts = {
        f"custom-object-type-{index}-{sentinel}-" + ("k" * 80): index
        for index in range(500)
    }
    huge_counts.update({
        "sources": 7,
        "memories": 3,
        "rules": 10 ** 5_000,
    })
    huge_summary = mcp_env["notebook"].model_copy(
        update={
            "name": "Notebook " + (sentinel * 2_000),
            "purpose": "Purpose " + (sentinel * 2_000),
            "counts": huge_counts,
            # PR#334:ask_notebook 现先经硬约束校验 get_notebook().ask_available;此桩
            # notebook 要可用,ask 才走到被 monkeypatch 的巨型响应(测预算截断)。
            "ask_available": True,
        }
    )
    monkeypatch.setattr(service, "get_notebook", lambda _notebook_id: huge_summary)
    monkeypatch.setattr(
        service,
        "unified_kg_status",
        lambda _notebook_id: {
            "dirty": True,
            "objects": 42,
            **{
                f"custom-status-{index}-{sentinel}-" + ("s" * 80): sentinel * 100
                for index in range(500)
            },
        },
    )

    record = service.create_memory_candidate(
        notebook_id,
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        "strict-budget-record",
        "Original",
        "Original",
        [],
        "test",
    ).model_copy(
        update={
            "title": sentinel * 2_000,
            "content_md": sentinel * 4_000,
            "tags": [sentinel * 500 for _ in range(100)],
            "provenance": {
                f"private-key-{index}-{sentinel}": {
                    "nested": [sentinel * 200 for _ in range(100)]
                }
                for index in range(100)
            },
        }
    )
    stale_hit = MemoryHit(
        memory_id=record.id,
        title=record.title,
        text=record.content_md,
        status="candidate",
        authority=1,
        score=1.0,
        provenance=record.provenance,
    )
    monkeypatch.setattr(service, "agent_memory_hits", lambda *args, **kwargs: [stale_hit])
    monkeypatch.setattr(service, "get_memory", lambda *args, **kwargs: record)
    monkeypatch.setattr(
        service,
        "search_notebook",
        lambda *args, **kwargs: SimpleNamespace(
            hits=[
                SimpleNamespace(
                    memory_id="",
                    scope="Source",
                    label=sentinel * 500,
                    text=sentinel * 2_000,
                    source_id=f"source-{index}",
                    element_id=f"element-{index}",
                    provenance={"nested": [sentinel * 100 for _ in range(100)]},
                )
                for index in range(20)
            ]
        ),
    )
    monkeypatch.setattr(
        service,
        "ask",
        lambda *args, **kwargs: SimpleNamespace(
            answer_id="answer-budget-id-" + ("a" * 20_000),
            answer=sentinel * 4_000,
            conclusion=sentinel * 2_000,
            grounded=True,
            evidence_level="source",
            mode="chunk",
            conversation_id="conversation-budget-id",
            anchors=[
                SimpleNamespace(
                    key=f"k_{index}",
                    object_id=f"object-{index}",
                    object_type="custom-" + ("t" * 1_000),
                    label=sentinel * 500,
                    source_title=sentinel * 500,
                    location_label=sentinel * 500,
                    source_id=f"anchor-source-{index}",
                    element_id=f"anchor-element-{index}",
                    tier="personal",
                    provenance={"nested": [sentinel * 100 for _ in range(100)]},
                    knowhow=(
                        SimpleNamespace(table_id="anchor-table-0", row_id="anchor-row-0")
                        if index == 0 else None
                    ),
                )
                for index in range(20)
            ],
            citations=[
                SimpleNamespace(
                    label=sentinel * 500,
                    source_id=f"citation-source-{index}",
                    element_id=f"citation-element-{index}",
                    location_label=sentinel * 500,
                    quoted_span=sentinel * 2_000,
                    source_file_name=sentinel * 500,
                    tier="personal",
                    notebook_id=f"citation-notebook-{index}",
                    memory_id="",
                    knowhow=(
                        SimpleNamespace(table_id="citation-table-0", row_id="citation-row-0")
                        if index == 0 else None
                    ),
                )
                for index in range(20)
            ],
        ),
    )
    proposal_record = record.model_copy(update={
        "id": "memory-" + ("m" * 20_000),
        "notebook_id": "notebook-" + ("n" * 20_000),
    })
    monkeypatch.setattr(
        service, "create_memory_candidate", lambda *args, **kwargs: proposal_record
    )

    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        responses = {
            "list_notebooks": _payload(await client.call("list_notebooks")),
            "select_notebook": _payload(await client.call(
                "select_notebook", {"notebook_id": notebook_id}
            )),
            "search_agent_memory": _payload(await client.call(
                "search_agent_memory", {"query": "budget", "limit": 20}
            )),
            "search_notebook_context": _payload(await client.call(
                "search_notebook_context", {"query": "budget", "limit": 20}
            )),
            "get_memory": _payload(await client.call(
                "get_memory", {"memory_id": record.id}
            )),
            "ask_notebook": _payload(await client.call(
                "ask_notebook", {"question": "budget", "mode": "chunk"}
            )),
            "propose_memory": _payload(await client.call(
                "propose_memory",
                {
                    "title": "Bounded proposal",
                    "content_md": "Bounded content",
                    "tags": ["bounded"],
                    "reason": "Bounded reason",
                    "task_context": {"task": "budget"},
                    "evidence_refs": [],
                    "client_request_id": "strict-budget-proposal",
                },
            )),
        }

    assert set(responses) == _MEMORY_TOOLS
    assert responses["list_notebooks"]["items"][0]["counts"]["sources"] == 7
    assert responses["list_notebooks"]["items"][0]["counts"]["memories"] == 3
    assert responses["select_notebook"]["kg_status"]["dirty"] is True
    assert responses["select_notebook"]["kg_status"]["objects"] == 42
    agent_items = responses["search_agent_memory"]["items"]
    assert sum(_json_chars(item["provenance"]) for item in agent_items) <= 2_000
    assert agent_items[0]["memory_id"] == record.id
    context_items = responses["search_notebook_context"]["items"]
    assert sum(_json_chars(item["provenance"]) for item in context_items) <= 2_000
    assert context_items[0]["source_id"] == "source-0"
    assert context_items[0]["element_id"] == "element-0"
    detail = responses["get_memory"]
    assert _json_chars(detail["provenance"]) <= 2_000
    assert _json_chars(detail["tags"]) <= 1_500
    assert detail["memory_id"] == record.id
    assert detail["notebook_id"] == notebook_id
    ask = responses["ask_notebook"]
    assert ask["conversation_id"] == "conversation-budget-id"
    assert _json_chars(ask["anchors"]) <= 3_500
    assert ask["anchors"]
    assert all(_json_chars(anchor["provenance"]) <= 500 for anchor in ask["anchors"])
    assert ask["anchors"][0]["key"] == "k_0"
    assert ask["anchors"][0]["object_id"] == "object-0"
    assert ask["anchors"][0]["source_id"] == "anchor-source-0"
    assert ask["anchors"][0]["element_id"] == "anchor-element-0"
    assert ask["citations"]
    assert all(
        citation["content_is_untrusted_evidence"] is True
        for citation in ask["citations"]
    )
    # The exact quoted_span (200)/source_file_name (300) field_limits are
    # covered by test_ask_notebook_projects_precise_anchor_and_citation_fields
    # with a small payload that never reaches the citations sub-budget fit or
    # the global convergence loop -- both of which crush every string well
    # under those limits here regardless of whether field_limits fires at
    # all, making an upper-bound assertion on this adversarial payload
    # vacuously true and unable to catch a dropped field_limits entry.
    for name, payload in responses.items():
        _assert_budgeted(payload)
        assert payload["truncation"]["truncated"] is True, name
        assert _omitted_total(payload) > 0, name


@pytest.mark.anyio
async def test_search_agent_memory_hydrates_fresh_records_and_drops_terminal_races(
    mcp_env, monkeypatch
):
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    stale_hits = []
    fresh_records = {}
    for suffix, fresh_status in (
        ("live", "confirmed"),
        ("rejected", "rejected"),
        ("deprecated", "deprecated"),
        ("deleted", None),
    ):
        item = service.create_memory_candidate(
            notebook_id,
            mcp_env["alice"].id,
            mcp_env["profile_a"].id,
            f"race-{suffix}",
            f"stored-{suffix}",
            f"stored-{suffix}",
            [],
            "test",
        )
        stale_hits.append(MemoryHit(
            memory_id=item.id,
            title=f"stale-title-{suffix}",
            text=f"stale-content-{suffix}",
            status="candidate",
            authority=1,
            score=0.9,
            provenance={"version": f"stale-{suffix}"},
        ))
        if fresh_status is not None:
            fresh_records[item.id] = item.model_copy(update={
                "status": fresh_status,
                "title": f"fresh-title-{suffix}",
                "content_md": f"fresh-content-{suffix}",
                "provenance": {"version": f"fresh-{suffix}"},
            })

    def fresh_get(memory_id, _owner_id):
        if memory_id not in fresh_records:
            raise KeyError(memory_id)
        return fresh_records[memory_id]

    monkeypatch.setattr(service, "agent_memory_hits", lambda *args, **kwargs: stale_hits)
    monkeypatch.setattr(service, "get_memory", fresh_get)

    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        result = _payload(await client.call(
            "search_agent_memory", {"query": "race", "limit": 20}
        ))

    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "confirmed"
    assert result["items"][0]["title"] == "fresh-title-live"
    assert result["items"][0]["content"] == "fresh-content-live"
    assert result["items"][0]["provenance"] == {"version": "fresh-live"}
    assert result["items"][0]["unconfirmed"] is False
    assert result["items"][0]["formal_notebook_conclusion"] is True


@pytest.mark.anyio
async def test_propose_memory_preserves_legitimate_nested_null_and_sdk_normalization(
    mcp_env,
):
    notebook_id = mcp_env["notebook"].id
    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        created = _payload(await client.call("propose_memory", {
            "title": "Nested null",
            "content_md": "Legitimate optional values remain null.",
            "tags": ["null"],
            "reason": "Optional evidence metadata",
            "task_context": {"optional": None, "nested": [{"value": None}]},
            "evidence_refs": [{"source_id": "source-null", "score": None}],
            "client_request_id": "nested-null-request",
        }))
        detail = _payload(await client.call(
            "get_memory", {"memory_id": created["memory_id"]}
        ))
        assert detail["provenance"]["task_context"] == {
            "nested": [{"value": None}],
            "optional": None,
        }
        assert detail["provenance"]["evidence_refs"] == [{
            "index": 0,
            "source_id": "source-null",
            "trusted": False,
            "type": "source",
            "validation": {
                "reason": "missing_or_cross_notebook",
                "status": "invalid",
            },
        }]

        # The official SDK serializes non-finite Python floats as JSON null.
        # The server cannot distinguish that normalization from legitimate null,
        # but no non-standard Infinity token reaches persistence or output.
        normalized = _payload(await client.call("propose_memory", {
            "title": "SDK normalized numeric",
            "content_md": "The official client emits null, not Infinity.",
            "tags": ["normalization"],
            "reason": "Protocol normalization probe",
            "task_context": {"value": float("inf")},
            "evidence_refs": [{"score": 1e9999}],
            "client_request_id": "normalized-nonfinite-request",
        }))
        normalized_detail = _payload(await client.call(
            "get_memory", {"memory_id": normalized["memory_id"]}
        ))
        assert normalized_detail["provenance"]["task_context"]["value"] is None
        assert normalized_detail["provenance"]["evidence_refs"] == [{
            "index": 0,
            "trusted": False,
            "type": "unsupported",
            "validation": {
                "reason": "unsupported_reference",
                "status": "invalid",
            },
        }]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e9999])
def test_proposal_helper_rejects_actual_nonfinite_nested_values(value):
    from app.api.mcp_server import _validate_proposal_input

    with pytest.raises(ValueError, match="JSON data|non-finite"):
        _validate_proposal_input(
            "Title",
            "Content",
            ["tag"],
            "Reason",
            {"nested": [{"value": value}]},
            [{"score": value}],
            "actual-nonfinite-request",
        )


@pytest.mark.anyio
async def test_propose_memory_rejects_unbounded_envelopes_before_live_service_work(
    mcp_env, monkeypatch
):
    from app.core.memory_inputs import (
        MEMORY_CONTENT_MAX_CHARS,
        MEMORY_EVIDENCE_MAX_COUNT,
        MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES,
        MEMORY_REASON_MAX_CHARS,
        MEMORY_TAG_MAX_CHARS,
        MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES,
        MEMORY_TITLE_MAX_CHARS,
    )

    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    base = {
        "title": "Proposal title",
        "content_md": "Proposal content",
        "tags": ["tag"],
        "reason": "Proposal reason",
        "task_context": {"task": "bounded"},
        "evidence_refs": [],
        "client_request_id": "bounded-request",
    }
    invalid_overrides = [
        {"title": "   "},
        {"content_md": "\n\t"},
        {"reason": "   "},
        {"task_context": {}},
        {"tags": [" "]},
        {"title": "t" * 100_000},
        {"content_md": "c" * 100_000},
        {"tags": ["tag"] * 1_000},
        {"tags": ["duplicate"] * 21},
        {"tags": ["t" * 100_000]},
        {"reason": "r" * 100_000},
        {"task_context": {"private": "x" * 100_000}},
        {"task_context": {"private": "证" * 3_000}},
        {"evidence_refs": [{"source_id": str(index)} for index in range(1_000)]},
        {"evidence_refs": [{"quote": "x" * 100_000}]},
        {"client_request_id": "r" * 100_000},
        {"title": "t" * (MEMORY_TITLE_MAX_CHARS + 1)},
        {"content_md": "c" * (MEMORY_CONTENT_MAX_CHARS + 1)},
        {"tags": ["t" * (MEMORY_TAG_MAX_CHARS + 1)]},
        {"reason": "r" * (MEMORY_REASON_MAX_CHARS + 1)},
        {
            "task_context": {
                "private": "x" * MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES
            }
        },
        {
            "evidence_refs": [
                {"source_id": str(index)}
                for index in range(MEMORY_EVIDENCE_MAX_COUNT + 1)
            ]
        },
        {
            "evidence_refs": [
                {"quote": "x" * MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES}
            ]
        },
    ]

    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        original_refresh = service.refresh_agent_principal
        live_calls = 0
        create_calls = 0

        def refresh_spy(*args, **kwargs):
            nonlocal live_calls
            live_calls += 1
            return original_refresh(*args, **kwargs)

        def create_spy(*args, **kwargs):
            nonlocal create_calls
            create_calls += 1
            raise AssertionError("invalid proposal reached Memory service")

        monkeypatch.setattr(service, "refresh_agent_principal", refresh_spy)
        monkeypatch.setattr(service, "create_memory_candidate", create_spy)
        for override in invalid_overrides:
            result = await client.call("propose_memory", {**base, **override})
            assert result.isError, override.keys()

    assert live_calls == 0
    assert create_calls == 0


@pytest.mark.anyio
async def test_propose_memory_accepts_payloads_within_exact_core_json_limits(mcp_env):
    notebook_id = mcp_env["notebook"].id
    task_context = {"private": "x" * 8_050}
    evidence_refs = [{"quote": "x" * 600} for _ in range(21)]

    # These values are deliberately above the removed MCP-only 8,000-byte,
    # 20-item, and 12,000-byte caps while remaining within the shared Core
    # 8,192-byte, 50-item, and 32,768-byte contract.
    assert len(json.dumps(
        task_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")) > 8_000
    assert len(evidence_refs) > 20
    assert len(json.dumps(
        evidence_refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")) > 12_000

    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        created = _payload(await client.call("propose_memory", {
            "title": "Exact Core envelope",
            "content_md": "Former MCP-only sub-budgets must not narrow Core.",
            "tags": ["core"],
            "reason": "Core limit regression",
            "task_context": task_context,
            "evidence_refs": evidence_refs,
            "client_request_id": "exact-core-envelope",
        }))

    assert created["status"] == "candidate"


@pytest.mark.anyio
async def test_propose_memory_delegates_exact_tag_normalization_to_core(mcp_env):
    notebook_id = mcp_env["notebook"].id
    raw_tags = [" analog ", "analog"] + [f"tag-{index}" for index in range(18)]
    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["token_a"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        created = _payload(await client.call("propose_memory", {
            "title": "Shared tag contract",
            "content_md": "Twenty raw values deduplicate after the raw cap.",
            "tags": raw_tags,
            "reason": "Tag parity regression",
            "task_context": {"task": "tag parity"},
            "evidence_refs": [],
            "client_request_id": "shared-tag-contract",
        }))
        detail = _payload(await client.call(
            "get_memory", {"memory_id": created["memory_id"]}
        ))

    assert detail["tags"] == [
        "analog", *[f"tag-{index}" for index in range(18)]
    ]


def test_nonlocal_plain_http_mcp_configuration_is_rejected():
    from app.api.mcp_server import validate_mcp_deployment

    validate_mcp_deployment("127.0.0.1", "http://127.0.0.1:8000/mcp")
    validate_mcp_deployment("0.0.0.0", "https://memory.example.test/mcp")
    with pytest.raises(RuntimeError, match="HTTPS"):
        validate_mcp_deployment("0.0.0.0", "http://memory.example.test/mcp")


@pytest.mark.anyio
async def test_transport_rejects_missing_token_and_untrusted_origin(mcp_env):
    app = mcp_env["app"]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }
    async with app.router.lifespan_context(app):
        await _wait_for_ready(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            follow_redirects=True,
        ) as http:
            missing = await http.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json=request,
            )
            hostile = await http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {mcp_env['token_a'].token}",
                    "Origin": "https://hostile.example",
                    "accept": "application/json, text/event-stream",
                },
                json=request,
            )
    assert missing.status_code == 401
    assert hostile.status_code == 403


@pytest.mark.anyio
async def test_runtime_transport_requires_https_only_for_remote_clients(mcp_env):
    app = mcp_env["app"]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "transport-test", "version": "1"},
        },
    }
    headers = {
        "Authorization": f"Bearer {mcp_env['token_a'].token}",
        "accept": "application/json, text/event-stream",
    }
    async with app.router.lifespan_context(app):
        await _wait_for_ready(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=("198.51.100.23", 43123)
            ),
            base_url="http://127.0.0.1",
            follow_redirects=True,
        ) as remote_http:
            rejected = await remote_http.post(
                "/mcp",
                headers={**headers, "X-Forwarded-Proto": "https"},
                json=request,
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=("198.51.100.23", 43123)
            ),
            base_url="https://127.0.0.1",
            follow_redirects=True,
        ) as remote_https:
            accepted_remote = await remote_https.post(
                "/mcp", headers=headers, json=request
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=("127.0.0.1", 43123)
            ),
            base_url="http://127.0.0.1",
            follow_redirects=True,
        ) as loopback_http:
            accepted_loopback = await loopback_http.post(
                "/mcp", headers=headers, json=request
            )
    assert rejected.status_code == 403
    assert accepted_remote.status_code == 200
    assert accepted_loopback.status_code == 200


# ===========================================================================
# knowhow-tables PR-2+3 Task 10: agent surface MCP tools (design doc §⑥) —
# same service core as app.api.knowhow_agent_routes's HTTP endpoints
# (app.services.knowhow.api), smoke-tested here via the same OfficialMcpClient
# harness the memory tools above use.
# ===========================================================================


def _seed_knowhow_table(notebook_id: str, *, anchor_text="过冲振铃", method_text="1. 看眼图\n2. 测阻抗"):
    from app.api.deps import repository
    repo = repository()
    table_id = repo.create_knowhow_table(
        notebook_id, "判别表", "",
        [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    table = repo.get_knowhow_table(table_id)
    anchor_id, method_id = (column["id"] for column in table["columns"])
    row_id = repo.add_knowhow_row(table_id, {anchor_id: anchor_text, method_id: method_text})
    return table_id, row_id, method_id


@pytest.mark.anyio
async def test_knowhow_tools_list_tables_and_discrimination(mcp_env):
    table_id, row_id, method_id = _seed_knowhow_table(mcp_env["notebook"].id)

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        await client.call("select_notebook", {"notebook_id": mcp_env["notebook"].id})
        tables = _payload(await client.call("list_knowhow_tables"))
        assert [item["id"] for item in tables["items"]] == [table_id]
        assert tables["items"][0]["anchor_column_id"]

        discrimination = _payload(
            await client.call("get_knowhow_discrimination", {"table_id": table_id})
        )
        assert len(discrimination["rows"]) == 1
        assert discrimination["rows"][0]["row_id"] == row_id
        assert discrimination["rows"][0]["methods"][0]["column_id"] == method_id
        assert discrimination["rows"][0]["methods"][0]["code_status"] == "none"


@pytest.mark.anyio
async def test_knowhow_tools_get_row_and_put_cell_code_is_scope_gated(mcp_env):
    table_id, row_id, method_id = _seed_knowhow_table(mcp_env["notebook"].id)
    app = mcp_env["app"]
    # Two sequential client sessions against the same app instance: the
    # underlying StreamableHTTPSessionManager can only be .run() once, so
    # (mirrors test_notebook_selection_is_required_and_session_scoped above)
    # share ONE outer lifespan and pass manage_lifespan=False to each client.
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as client:
            await client.call("select_notebook", {"notebook_id": mcp_env["notebook"].id})
            row = _payload(await client.call("get_knowhow_row", {"row_id": row_id}))
            assert row["title"] == "过冲振铃"
            assert row["code"] == []

            # token_a's scopes (knowledge:read/memory:*/ask:execute) do not
            # include knowhow:code -> the write is rejected.
            denied = await client.call(
                "put_knowhow_cell_code",
                {
                    "row_id": row_id, "column_id": method_id,
                    "code_text": "print(1)", "language": "python",
                },
            )
            assert denied.isError

        # A second token WITH knowhow:code can write; get_knowhow_row then
        # surfaces the attachment (including updated_by attribution).
        code_token = mcp_env["service"].issue_agent_token(
            mcp_env["alice"].id, mcp_env["profile_a"].id,
            ["knowledge:read", "knowhow:code"], mcp_env["notebook"].id,
            [mcp_env["notebook"].id], None,
        )
        async with OfficialMcpClient(
            app, code_token.token, manage_lifespan=False
        ) as client:
            await client.call("select_notebook", {"notebook_id": mcp_env["notebook"].id})
            put_result = _payload(await client.call(
                "put_knowhow_cell_code",
                {
                    "row_id": row_id, "column_id": method_id,
                    "code_text": "print(1)", "language": "python",
                },
            ))
            assert put_result["status"] == "implemented"

            row = _payload(await client.call("get_knowhow_row", {"row_id": row_id}))
            assert row["code"][0]["column_id"] == method_id
            assert row["code"][0]["status"] == "implemented"
            assert row["code"][0]["updated_by"] == mcp_env["profile_a"].name


@pytest.mark.anyio
async def test_knowhow_tools_reject_a_row_from_a_notebook_outside_the_session(mcp_env):
    """Data-isolation cross-check (mirrors get_memory's own notebook_id
    mismatch guard): a row_id resolved to a DIFFERENT notebook than the one
    currently selected on this MCP session must not be readable, even though
    the token's own allowlist covers both notebooks."""
    other_table_id, other_row_id, other_method_id = _seed_knowhow_table(mcp_env["other"].id)
    both_notebooks_token = mcp_env["service"].issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id, ["knowledge:read"],
        mcp_env["notebook"].id, [mcp_env["notebook"].id, mcp_env["other"].id], None,
    )

    async with OfficialMcpClient(mcp_env["app"], both_notebooks_token.token) as client:
        await client.call("select_notebook", {"notebook_id": mcp_env["notebook"].id})
        row_result = await client.call("get_knowhow_row", {"row_id": other_row_id})
        assert row_result.isError
        discrimination_result = await client.call(
            "get_knowhow_discrimination", {"table_id": other_table_id}
        )
        assert discrimination_result.isError


@pytest.mark.anyio
async def test_knowhow_agent_tool_responses_respect_serialized_budget(mcp_env):
    """Mirrors test_all_seven_official_client_tool_responses_have_strict_
    serialized_budgets's adversarial-data style at a smaller scale: an
    oversized cell/code payload must still come back within MCP_OUTPUT_BUDGET
    with truncation stats, never a raw serialization failure (design doc §⑥:
    "响应受既有 _budget_response 预算约束")."""
    sentinel = "私密-knowhow-budget-sentinel"
    table_id, row_id, method_id = _seed_knowhow_table(
        mcp_env["notebook"].id,
        anchor_text=sentinel * 500,
        method_text="1. " + (sentinel * 2_000),
    )
    code_token = mcp_env["service"].issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id,
        ["knowledge:read", "knowhow:code"], mcp_env["notebook"].id,
        [mcp_env["notebook"].id], None,
    )

    async with OfficialMcpClient(mcp_env["app"], code_token.token) as client:
        await client.call("select_notebook", {"notebook_id": mcp_env["notebook"].id})

        discrimination = _payload(
            await client.call("get_knowhow_discrimination", {"table_id": table_id})
        )
        _assert_budgeted(discrimination)

        put_result = _payload(await client.call(
            "put_knowhow_cell_code",
            {
                "row_id": row_id, "column_id": method_id,
                "code_text": sentinel * 4_000, "language": "python",
            },
        ))
        _assert_budgeted(put_result)

        row = _payload(await client.call("get_knowhow_row", {"row_id": row_id}))
        _assert_budgeted(row)


# --- get_cited_element: 引用点查 -------------------------------------------
# 界面上「点 [k] 看原文」的 MCP 等价物。能力面刻意不超过一次 Ask 已经授权披露的
# 范围,所以它复用的是浏览器代理读取那条同一份判据
# (`source_routes.source_readable_in_participant_scope`),而不是另写一遍。
_CITED_NOW = "2026-05-05T06:07:08"


def _seed_cited_source(
    repo,
    notebook_id: str,
    tag: str,
    *,
    source_type: str = "document",
    title: str = "",
    element_type: str = "formula",
    location_label: str = "第 2 页",
    text: str = "E = mc^2",
    element_metadata: str = "{}",
    paper_title: str = "",
) -> dict:
    """在 ``notebook_id`` 里插一条最小可读来源(1 个元素)。

    直插行而不跑摄取管线:本组用例只关心**授权口径与投影字段**,解析/嵌入/抽取
    都与之无关(与 test_multi_domain_bases 里代理读取那组同款做法)。
    """
    source_id = f"src-{tag}"
    element_id = f"el-{tag}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, title or f"{tag} 讲义", source_type,
             "ready", _CITED_NOW, _CITED_NOW),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (element_id, source_id, element_type, location_label, text,
             element_metadata, _CITED_NOW),
        )
        if paper_title:
            db.execute(
                "INSERT INTO source_paper_meta "
                "(source_id,notebook_id,is_paper,paper_title,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (source_id, notebook_id, 1, paper_title, _CITED_NOW, _CITED_NOW),
            )
    return {"source_id": source_id, "element_id": element_id}


@pytest.mark.anyio
async def test_get_cited_element_dereferences_a_citation_to_its_source_text(mcp_env):
    """Happy path with exact values (not presence checks) for every projected
    field, so a mutation to any one of them shows up here:
      - the element's own type/text/location, keyed by the id that was asked
        for (never the source's first element, see the miss cases below);
      - ``source_title`` taken from the ONE display-title rule
        (``source_display.source_display_title``): a grounded paper title
        outranks the uploaded file name, which is exactly what a second,
        locally re-written title rule would get wrong;
      - the knowhow row pointer when the element carries one, absent when not;
      - ``notebook_id`` omitted for same-notebook evidence;
      - no ``file_path``/``error_message``, mirroring ``ScopedSourceDetail``.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    plain = _seed_cited_source(repo, notebook_id, "cited-plain")
    cell = _seed_cited_source(
        repo, notebook_id, "cited-knowhow",
        element_type="knowhow_cell", location_label="第 3 行 · 步骤",
        text="先做 kelvin 保护环",
        element_metadata=json.dumps(
            {"knowhow": {"table_id": "tbl-7", "row_id": "row-9"}}
        ),
    )
    paper = _seed_cited_source(
        repo, notebook_id, "cited-paper",
        title="1706.03762.pdf", paper_title="Attention Is All You Need",
    )

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))

        payload = _payload(await client.call("get_cited_element", {
            "source_id": plain["source_id"], "element_id": plain["element_id"],
        }))
        assert payload["source_id"] == "src-cited-plain"
        assert payload["element_id"] == "el-cited-plain"
        assert payload["element_type"] == "formula"
        assert payload["text"] == "E = mc^2"
        assert payload["location_label"] == "第 2 页"
        assert payload["source_title"] == "cited-plain 讲义"
        assert payload["content_is_untrusted_evidence"] is True
        assert "notebook_id" not in payload, "同库证据不复述 Agent 自己选的库"
        assert "knowhow" not in payload, "普通元素没有可跳的格子"
        assert "file_path" not in payload and "error_message" not in payload
        _assert_budgeted(payload)

        knowhow_payload = _payload(await client.call("get_cited_element", {
            "source_id": cell["source_id"], "element_id": cell["element_id"],
        }))
        assert knowhow_payload["knowhow"] == {"table_id": "tbl-7", "row_id": "row-9"}
        assert knowhow_payload["element_type"] == "knowhow_cell"

        paper_payload = _payload(await client.call("get_cited_element", {
            "source_id": paper["source_id"], "element_id": paper["element_id"],
        }))
        assert paper_payload["source_title"] == "Attention Is All You Need"


@pytest.mark.anyio
async def test_get_cited_element_rejects_ids_it_cannot_ground(mcp_env):
    """Three misses that must all fail rather than return *something*.

    The middle one is the load-bearing case, and it is an AUTHORIZATION check,
    not tidiness: ``evidence_elements`` looks an element up by its own id with
    no source predicate at all, so pairing source A's id with source B's
    element id would hand back B's text under A's title. That pairing is also
    exactly how a caller would probe a source it is not allowed to open. The
    unknown-source case stays indistinguishable from the unauthorized one (no
    existence disclosure).
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    plain = _seed_cited_source(repo, notebook_id, "miss-plain")
    elsewhere = _seed_cited_source(
        repo, notebook_id, "miss-elsewhere", text="别的来源的正文"
    )

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))

        assert (await client.call("get_cited_element", {
            "source_id": plain["source_id"], "element_id": "el-does-not-exist",
        })).isError
        crossed = await client.call("get_cited_element", {
            "source_id": plain["source_id"],
            "element_id": elsewhere["element_id"],
        })
        assert crossed.isError, "元素必须属于入参 source,否则就是一次跨来源探测"
        assert "别的来源的正文" not in json.dumps(
            [item.text for item in crossed.content], ensure_ascii=False
        )
        assert (await client.call("get_cited_element", {
            "source_id": "src-does-not-exist",
            "element_id": plain["element_id"],
        })).isError


@pytest.mark.anyio
async def test_get_cited_element_requires_knowledge_read_and_a_selected_notebook(
    mcp_env,
):
    """The two authentication gates every data tool shares, on this one:
    a token without ``knowledge:read`` (the restricted fixture holds only
    ``memory:read``) and a session that never called ``select_notebook``."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = _seed_cited_source(repo, notebook_id, "gated")
    arguments = {
        "source_id": seeded["source_id"], "element_id": seeded["element_id"],
    }

    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["restricted"].token, manage_lifespan=False
        ) as restricted:
            _payload(await restricted.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            assert (await restricted.call("get_cited_element", arguments)).isError

        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as client:
            assert (await client.call("get_cited_element", arguments)).isError
            _payload(await client.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            assert not (await client.call("get_cited_element", arguments)).isError


@pytest.mark.anyio
async def test_get_cited_element_follows_the_participant_set_not_the_allowlist(
    mcp_env,
):
    """Scope is the SELECTED notebook's live participant set (itself plus the
    reference libraries it currently mounts) — NOT the token's allowlist.

    The allowlist deliberately does NOT cover ``other`` and DOES cover a third
    notebook that is never mounted, so the two axes are pinned in both
    directions and neither can be mistaken for the other:
      - in the allowlist, not a participant (``unmounted``) → denied. "This
        token may select that notebook" must never double as "that notebook's
        sources are readable from here".
      - not in the allowlist, but mounted (``other``) → readable, and it
        reports the library the source really belongs to (``notebook_id``),
        which is how the Agent knows the quote is not from the notebook it
        selected. Reading the allowlist instead of the mount would deny this.
    Plus: a hidden synthetic (memory/knowhow) source stays unreachable ACROSS
    libraries — Memory is owner-private and a projection row is not a user
    document — while the same-notebook case keeps its existing behaviour
    (tightening that belongs to its own change).
    """
    repo = repository()
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    other_id = mcp_env["other"].id
    marker = set_request_user(mcp_env["alice"])
    try:
        unmounted = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Allowlisted but never mounted")
        )
    finally:
        reset_request_user(marker)
    base_doc = _seed_cited_source(
        repo, other_id, "base-doc", text="参考库里的原文", location_label="第 9 页"
    )
    base_memory = _seed_cited_source(
        repo, other_id, "base-memory", source_type="memory"
    )
    own_memory = _seed_cited_source(
        repo, notebook_id, "own-memory", source_type="memory"
    )
    unmounted_doc = _seed_cited_source(repo, unmounted.id, "allowlisted-only")
    scoped_token = service.issue_agent_token(
        mcp_env["alice"].id, mcp_env["profile_a"].id, ["knowledge:read"],
        notebook_id, [notebook_id, unmounted.id], None,
    )

    async with OfficialMcpClient(mcp_env["app"], scoped_token.token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))

        assert (await client.call("get_cited_element", {
            "source_id": unmounted_doc["source_id"],
            "element_id": unmounted_doc["element_id"],
        })).isError, "在 allowlist 里但不在参与集内:能选中 ≠ 来源可读"
        assert (await client.call("get_cited_element", {
            "source_id": base_doc["source_id"],
            "element_id": base_doc["element_id"],
        })).isError, "未挂载时,别的库的来源与不存在同义"

        service.replace_notebook_bases(notebook_id, [other_id], mcp_env["alice"].id)

        mounted = _payload(await client.call("get_cited_element", {
            "source_id": base_doc["source_id"],
            "element_id": base_doc["element_id"],
        }))
        assert mounted["text"] == "参考库里的原文"
        assert mounted["location_label"] == "第 9 页"
        assert mounted["notebook_id"] == other_id
        assert "file_path" not in mounted and "error_message" not in mounted
        _assert_budgeted(mounted)

        # The mount did not widen the allowlist axis back open either.
        assert (await client.call("get_cited_element", {
            "source_id": unmounted_doc["source_id"],
            "element_id": unmounted_doc["element_id"],
        })).isError

        assert (await client.call("get_cited_element", {
            "source_id": base_memory["source_id"],
            "element_id": base_memory["element_id"],
        })).isError, "跨库的隐藏合成源一律不代理"
        assert not (await client.call("get_cited_element", {
            "source_id": own_memory["source_id"],
            "element_id": own_memory["element_id"],
        })).isError, "同库合成源保持既有行为不变"


# --------------------------------------------------------------------------- #
# Agent source management: add_source_text / add_source_url / get_source_status
# / reparse_source / delete_source.
# --------------------------------------------------------------------------- #
_SOURCE_READ = ["knowledge:read"]
_SOURCE_WRITE = ["knowledge:read", "sources:write"]
_SOURCE_DELETE = ["knowledge:read", "sources:delete"]
_SOURCE_FULL = ["knowledge:read", "sources:write", "sources:delete"]


def _agent_token(
    mcp_env, scopes, *, user="alice", notebook_ids=None, profile=None
) -> str:
    """Issue a raw Agent token for ``user`` with exactly ``scopes``.

    The mcp_env fixture's own tokens deliberately carry only the read/propose
    scopes, so every source test mints its own — which is also how each test
    pins that one scope, and no more, is what opens its tool. ``profile``
    selects a specific Agent profile of that user (both of Alice's profiles are
    needed to pin the cross-profile delete rule).
    """
    notebook_ids = notebook_ids or [mcp_env["notebook"].id]
    if profile is None:
        profile = mcp_env["profile_a"] if user == "alice" else mcp_env["bob_profile"]
    return mcp_env["service"].issue_agent_token(
        mcp_env[user].id, profile.id, scopes,
        notebook_ids[0], notebook_ids, None,
    ).token


@pytest.fixture
def reachable_pdf_url(monkeypatch):
    """Make add_source_url's happy path reachable WITHOUT touching the network.

    Two stubs, both required: the ingestion service refuses outright unless a
    MinerU parser is configured (the fixture clears every MINERU_* var), and
    ``probe_pdf`` is what would otherwise issue a real HTTP request. Returns the
    URL the stubs accept.
    """
    from app.services import remote_sources

    repo = repository()
    monkeypatch.setattr(
        repo._runtime.source_ingestion, "mineru_cloud_client",
        lambda: SimpleNamespace(configured=True), raising=False,
    )
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kwargs: remote_sources.PdfProbe(True, "", 4096, "手册.pdf"),
    )
    return "https://example.test/handbook.pdf"


@pytest.fixture
def scheduled_jobs(monkeypatch):
    """Capture background submissions instead of running them.

    Both add_source_text and reparse_source hand parsing to
    ``kg_scheduler.submit_job``. Letting that run would start a real
    parse/embed/extract pipeline on a worker thread mid-test; capturing it
    keeps these tests about the tool contract AND lets them assert that the
    work was actually queued (which a swallowed submit could not be
    distinguished from).
    """
    from app.services.kg import scheduler as kg_scheduler

    calls: list = []
    monkeypatch.setattr(
        kg_scheduler, "submit_job",
        lambda fn, *args, **kwargs: calls.append((fn, args)),
    )
    return calls


def _stored_provenance(repo, source_id: str):
    """The RAW v48 column — the thing `agent_created` is derived from."""
    with repo._connect() as db:
        row = db.execute(
            "SELECT agent_profile_id FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return None if row is None else row["agent_profile_id"]


def _source_row_exists(repo, source_id: str) -> bool:
    with repo._connect() as db:
        return db.execute(
            "SELECT 1 FROM sources WHERE id = ?", (source_id,)
        ).fetchone() is not None


@pytest.mark.anyio
async def test_add_source_text_files_agent_authored_markdown(mcp_env, scheduled_jobs):
    """Happy path with exact values: the row exists, carries this Agent's
    profile id in the raw provenance column, is queued for the ordinary
    background parse, and reports itself as Agent-added."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call("add_source_text", {
            "title": "时钟树收敛方案",
            "content_md": "# 时钟树\n\n先做 useful skew。\n",
        }))

    assert payload["reused"] is False
    assert payload["agent_created"] is True
    assert payload["parse_status"] == "queued"
    # The stored title is the SUBMITTED title, verbatim — not the derived file
    # name (which is sanitized, byte-truncated and suffixed).
    assert payload["title"] == "时钟树收敛方案"
    _assert_budgeted(payload)

    source_id = payload["source_id"]
    assert _stored_provenance(repo, source_id) == mcp_env["profile_a"].id
    detail = repo.get_source(source_id)
    assert detail.notebook_id == notebook_id
    assert detail.agent_created is True
    assert detail.title == "时钟树收敛方案"
    assert detail.file_name == "时钟树收敛方案.md"
    # Queued through the ordinary ingestion scheduler, not a bespoke path.
    assert [args for _fn, args in scheduled_jobs] == [(source_id,)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("file_name", "payload"),
    [
        ("manual.pdf", b"%PDF-1.7\nagent upload\n"),
        ("slides.pptx", b"PK\x03\x04pptx-placeholder"),
        ("workbook.xlsx", b"PK\x03\x04xlsx-placeholder"),
        ("notes.zip", b"PK\x03\x04zip-placeholder"),
    ],
)
async def test_add_source_file_accepts_registered_binary_formats(
    mcp_env, scheduled_jobs, file_name, payload
):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        result = _payload(await client.call("add_source_file", {
            "file_name": file_name,
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }))

    detail = repo.get_source(result["source_id"])
    assert detail.file_name == file_name
    assert detail.title == file_name
    assert detail.agent_created is True
    assert pathlib.Path(detail.file_path).read_bytes() == payload
    assert scheduled_jobs[-1][1] == (result["source_id"],)


@pytest.mark.anyio
async def test_add_source_file_rejects_invalid_envelopes_before_repository_work(
    mcp_env, monkeypatch, scheduled_jobs
):
    from app.api.mcp_tools import sources

    repo = repository()
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(
        sources, "get_settings",
        lambda: SimpleNamespace(source_upload_max_bytes=8),
    )
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        for arguments in (
            {"file_name": "bad.exe", "content_base64": "YQ=="},
            {"file_name": "paper.pdf", "content_base64": "not base64"},
            {"file_name": "paper.pdf", "content_base64": ""},
            {
                "file_name": "paper.pdf",
                "content_base64": base64.b64encode(b"123456789").decode("ascii"),
            },
        ):
            assert (await client.call("add_source_file", arguments)).isError

    assert repo.list_sources(notebook_id) == []
    assert scheduled_jobs == []


@pytest.mark.anyio
async def test_add_source_text_reusing_a_user_source_does_not_claim_it(
    mcp_env, scheduled_jobs
):
    """Content dedup must not launder provenance.

    A person uploads bytes; the Agent later files the identical text. The
    existing row is reused (no duplicate document), and it stays user-added —
    which is precisely what keeps delete_source from removing it afterwards.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    body = "# 共享内容\n\n同样的字节。\n"
    marker = set_request_user(mcp_env["alice"])
    try:
        from app.repositories.ports import UploadedSourceFile

        human = repo.upload_sources(
            notebook_id,
            [UploadedSourceFile("人写的.md", "text/markdown", body.encode("utf-8"))],
            lambda source_id: None,
        )
    finally:
        reset_request_user(marker)
    assert _stored_provenance(repo, human[0].id) is None

    token = _agent_token(mcp_env, _SOURCE_FULL)
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(app, token, manage_lifespan=False) as client:
            _payload(await client.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            payload = _payload(await client.call("add_source_text", {
                "title": "换个标题", "content_md": body,
            }))
            assert payload["source_id"] == human[0].id
            assert payload["reused"] is True
            assert payload["agent_created"] is False, (
                "复用既有行不得改写出处——否则一次重传就能拿到删除权"
            )
            # And the reused row is genuinely still undeletable through MCP.
            refused = await client.call(
                "delete_source", {"source_id": human[0].id}
            )
            assert refused.isError

    assert _stored_provenance(repo, human[0].id) is None
    assert _source_row_exists(repo, human[0].id)
    assert len(repo.list_sources(notebook_id)) == 1


@pytest.mark.anyio
async def test_add_source_text_rejects_bad_envelopes_before_touching_the_repo(
    mcp_env, monkeypatch, scheduled_jobs
):
    """Blank title, blank body and an over-limit body all fail, and none of
    them creates a row. The size ceiling is the deployment's own
    ``source_upload_max_bytes``, measured on UTF-8 BYTES — a character count
    would let a CJK body through at three times the configured limit."""
    from app.api.mcp_tools import sources

    repo = repository()
    notebook_id = mcp_env["notebook"].id
    monkeypatch.setattr(
        sources, "get_settings",
        lambda: SimpleNamespace(source_upload_max_bytes=64),
    )
    # 30 characters, 90 UTF-8 bytes: under a naive character check, over the
    # real byte budget.
    oversized = "很长的中文正文内容需要被拒绝掉哦" * 2

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        assert (await client.call("add_source_text", {
            "title": "   ", "content_md": "有内容",
        })).isError
        assert (await client.call("add_source_text", {
            "title": "标题", "content_md": "   \n  ",
        })).isError
        too_big = await client.call("add_source_text", {
            "title": "标题", "content_md": oversized,
        })
        assert too_big.isError
        assert len(oversized) < 64 < len(oversized.encode("utf-8"))
        # Just under the same ceiling still lands.
        assert not (await client.call("add_source_text", {
            "title": "标题", "content_md": "ok",
        })).isError

    assert len(repo.list_sources(notebook_id)) == 1


@pytest.mark.anyio
async def test_add_source_text_keeps_the_stored_file_name_within_the_fs_limit(
    mcp_env, scheduled_jobs
):
    """A maximum-length CJK title must still produce a WRITABLE file name.

    The budget belongs to the name ingestion actually writes —
    ``SourceFileStore.write_upload`` stores ``f"{source_id}_{safe_filename}"``,
    so the ``src-`` + 32-hex id and its separator (37 bytes) come out of the
    same 255-byte path component. Asserting on the bare stem instead is how a
    budget that overflows by exactly that prefix passes review.

    ⚠ This cannot fail on a dev Mac even when it is wrong: APFS/HFS+ count 255
    UTF-16 units, ext4/XFS/NTFS count 255 BYTES, and a 200-character CJK title
    is 600 bytes. So the assertion is arithmetic on the composed name, not "did
    the write raise" — the latter is green here and red in Linux production.
    """
    from app.api.mcp_server import SOURCE_TITLE_MAX_CHARS

    repo = repository()
    notebook_id = mcp_env["notebook"].id
    longest = "笔" * SOURCE_TITLE_MAX_CHARS
    assert len(longest.encode("utf-8")) == SOURCE_TITLE_MAX_CHARS * 3

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call("add_source_text", {
            "title": longest, "content_md": "正文",
        }))
        # Over-long titles are SHORTENED, never refused: the file write happens
        # before the row insert, so the alternative is an OSError the caller can
        # do nothing about, carrying the storage absolute path in its message.
        assert payload["reused"] is False

    source_id = payload["source_id"]
    detail = repo.get_source(source_id)
    stored = pathlib.Path(detail.file_path).name
    assert len(stored.encode("utf-8")) <= 255, (
        f"落盘组件 {len(stored.encode('utf-8'))} 字节 > 255,Linux 上会 OSError"
    )
    # Pin the composition too, so the check keeps holding for the longest id
    # this scheme can mint rather than for whichever uuid this run happened to
    # draw (they are fixed-width today, and this states that dependency).
    assert stored.startswith(f"{source_id}_") and stored.endswith(".md")
    widest = f"src-{'0' * 32}_{stored.split('_', 1)[1]}"
    assert len(widest.encode("utf-8")) <= 255
    # Truncation is byte-safe: no character was split mid-sequence.
    assert stored.encode("utf-8").decode("utf-8") == stored
    # ⚠ And the shortening stops at the FILE NAME. The stored title is the
    # submitted title in full — read back from the row, not echoed from the
    # response — because the file name is derived from the title and feeding it
    # back into `sources.title` would put a truncated path string where the
    # user's own words belong.
    assert detail.title == longest
    assert len(repo.list_sources(notebook_id)) == 1


@pytest.mark.anyio
async def test_add_source_text_stores_the_submitted_title_not_the_file_name(
    mcp_env, scheduled_jobs
):
    """The title and the file name are two different values.

    ``upload_sources`` historically wrote ``title=file_name`` because for a
    browser upload they ARE the same thing. Here they are not: the file name is
    the title after ``safe_filename`` (separators flattened), a byte budget, and
    a ``.md`` suffix. A title carrying path characters therefore shows exactly
    how much a shared field would corrupt.

    Read back from the stored row, not from the tool's own reply — echoing the
    request would pass even if nothing reached the database.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    title = "docs/2026 Q3: 时序收敛/复盘"

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call("add_source_text", {
            "title": title, "content_md": "正文内容",
        }))

    detail = repo.get_source(payload["source_id"])
    assert detail.title == title
    assert repo.list_sources(notebook_id)[0].title == title
    # The file name is the sanitized derivation and is NOT what landed in the
    # title. `safe_filename` keeps only the FINAL path component, so sharing one
    # field would have reduced this title to a two-character stub — the whole
    # of "docs/2026 Q3: 时序收敛/" silently gone from the sources tab.
    assert detail.file_name == "复盘.md"
    assert detail.file_name != detail.title
    assert pathlib.Path(detail.file_path).name.endswith(detail.file_name)


@pytest.mark.anyio
async def test_add_source_text_honours_the_notebook_document_limit(
    mcp_env, scheduled_jobs, reachable_pdf_url
):
    """The per-notebook document ceiling lives in the ROUTER, not in
    ``upload_sources`` — so an Agent create path that skipped it would simply
    have no limit at all. Squeeze the owner's effective limit down to one and
    watch the second document be refused."""
    repo = repository()
    identity = identity_repository()
    notebook_id = mcp_env["notebook"].id
    identity.set_user_document_limit_override("user-local", mcp_env["alice"].id, 1)

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        assert not (await client.call("add_source_text", {
            "title": "第一篇", "content_md": "第一篇正文",
        })).isError
        full = await client.call("add_source_text", {
            "title": "第二篇", "content_md": "第二篇正文",
        })
        assert full.isError
        # The message is the browser route's, verbatim -- one condition must
        # not read one way in the web UI and another way to an Agent (and it
        # must not be English, which the frontend error layer would swallow).
        assert document_capacity_message(1, 1, 1) in full.content[0].text
        # add_source_url refuses the same way rather than passing through the
        # ingestion service's separate over-limit wording.
        url_full = await client.call(
            "add_source_url", {"url": reachable_pdf_url}
        )
        assert url_full.isError
        assert document_capacity_message(1, 1, 1) in url_full.content[0].text

    assert len(repo.list_sources(notebook_id)) == 1


@pytest.mark.anyio
async def test_add_source_text_still_dedups_when_the_notebook_is_full(
    mcp_env, scheduled_jobs
):
    """A full notebook must not break the documented idempotence.

    "Re-adding byte-identical content returns the existing source" is the tool's
    contract, and a reuse adds NO document — so refusing it on the document
    ceiling is refusing a call that would not have consumed the quota. That
    matters precisely at the limit, which is the one state where an Agent most
    needs a repeat call to be a safe no-op (retry after a dropped response,
    resumed run, …).

    So the ceiling is checked only after the content hash misses. New content is
    still refused, which is what keeps this from being a hole in the quota.
    """
    repo = repository()
    identity = identity_repository()
    notebook_id = mcp_env["notebook"].id
    body = "# 已经存在的内容\n\n同样的字节。\n"

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        first = _payload(await client.call("add_source_text", {
            "title": "第一篇", "content_md": body,
        }))
        assert first["reused"] is False

        # Now the notebook is exactly full.
        identity.set_user_document_limit_override("user-local", mcp_env["alice"].id, 1)
        assert (await client.call("add_source_text", {
            "title": "新内容", "content_md": "完全不同的正文",
        })).isError, "满库仍然拒绝真正新增的文档"

        # Same bytes, different title: still a reuse, still allowed.
        again = _payload(await client.call("add_source_text", {
            "title": "换个标题重试", "content_md": body,
        }))
        assert again["reused"] is True
        assert again["source_id"] == first["source_id"]

    assert len(repo.list_sources(notebook_id)) == 1


@pytest.mark.anyio
async def test_add_source_url_explains_a_rejected_url_and_a_missing_parser(
    mcp_env, scheduled_jobs
):
    """Two refusals, neither of which touches the network.

    ``MINERU_*`` is cleared by the fixture, so the first call reproduces a
    deployment with no PDF parser and must say so without naming the
    deployment's environment variables. With a stub parser configured, an
    ``ftp://`` URL is rejected by ``probe_pdf``'s scheme check — which returns
    before any request is made — and the caller gets that reason back.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    token = _agent_token(mcp_env, _SOURCE_WRITE)
    app = mcp_env["app"]

    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(app, token, manage_lifespan=False) as client:
            _payload(await client.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            unconfigured = await client.call(
                "add_source_url", {"url": "https://example.test/a.pdf"}
            )
            assert unconfigured.isError
            text = unconfigured.content[0].text
            assert "MINERU" not in text.upper()

            repo._runtime.source_ingestion.mineru_cloud_client = (
                lambda: SimpleNamespace(configured=True)
            )
            try:
                rejected = await client.call(
                    "add_source_url", {"url": "ftp://example.test/a.pdf"}
                )
                assert rejected.isError
                assert "http" in rejected.content[0].text
                assert (await client.call("add_source_url", {"url": "  "})).isError
            finally:
                del repo._runtime.source_ingestion.mineru_cloud_client

    assert repo.list_sources(notebook_id) == []
    assert scheduled_jobs == []


@pytest.mark.anyio
async def test_add_source_url_files_an_agent_owned_pdf(
    mcp_env, scheduled_jobs, reachable_pdf_url
):
    """The success path, with the network stubbed out at both gates.

    Exact values for all five projected fields — the refusal cases above only
    ever exercise the error branch, so without this the shape an Agent actually
    consumes on success is unasserted.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call(
            "add_source_url", {"url": reachable_pdf_url}
        ))

    assert set(payload) == {
        "source_id", "title", "parse_status", "status", "agent_created",
        "truncation",
    }
    assert payload["title"] == "手册.pdf"
    assert payload["parse_status"] == "queued"
    assert payload["status"] == "queued"
    assert payload["agent_created"] is True
    _assert_budgeted(payload)

    source_id = payload["source_id"]
    assert _stored_provenance(repo, source_id) == mcp_env["profile_a"].id
    detail = repo.get_source(source_id)
    assert detail.notebook_id == notebook_id
    assert detail.source_url == reachable_pdf_url
    # Queued through the ordinary ingestion scheduler, like the browser route.
    assert [args for _fn, args in scheduled_jobs] == [(source_id,)]


@pytest.mark.anyio
async def test_get_source_status_projects_state_without_the_private_diagnostics(
    mcp_env,
):
    """The projection is exactly the parse/extraction state, plus a DERIVED
    ``parse_failed`` boolean. ``error_message`` is ``str(exc)`` stored verbatim
    and routinely carries server-side absolute paths, so it never appears —
    the same narrowing ``ScopedSourceDetail`` applies to the browser's proxy
    read. ``file_path`` likewise."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = _seed_cited_source(repo, notebook_id, "status-ok")
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET parse_status='failed', error_message=? WHERE id=?",
            ("/Users/secret/storage/notebooks/x.pdf 打不开", seeded["source_id"]),
        )

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_READ)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call(
            "get_source_status", {"source_id": seeded["source_id"]}
        ))

    assert payload["source_id"] == seeded["source_id"]
    assert payload["parse_status"] == "failed"
    assert payload["parse_failed"] is True
    assert payload["element_count"] == 1
    assert payload["kg_extracted"] is False
    assert payload["agent_created"] is False
    assert payload["parse_quality_warning"] is False
    assert "error_message" not in payload and "file_path" not in payload
    assert "/Users/secret" not in json.dumps(payload, ensure_ascii=False)
    _assert_budgeted(payload)


@pytest.mark.anyio
async def test_source_tools_see_the_selected_notebook_only_never_the_mounted_set(
    mcp_env,
):
    """Scope for these tools is the SELECTED notebook itself.

    ``get_cited_element`` reads across the participant set because an answer
    may quote a mounted reference library. Managing documents is the opposite:
    a mounted library's sources belong to another notebook whose owner never
    agreed to an Agent re-parsing or deleting them. So ``other`` is mounted
    here — proving the refusal comes from the notebook boundary and not from
    the source being unreachable — and hidden memory/knowhow projection rows
    are refused even inside the selected notebook.
    """
    repo = repository()
    service = mcp_env["service"]
    notebook_id = mcp_env["notebook"].id
    other_id = mcp_env["other"].id
    mounted = _seed_cited_source(repo, other_id, "mounted-doc")
    synthetic = _seed_cited_source(
        repo, notebook_id, "own-synthetic", source_type="memory"
    )
    service.replace_notebook_bases(notebook_id, [other_id], mcp_env["alice"].id)
    # The mount really is live: the citation tool CAN read that same source.
    token = _agent_token(mcp_env, _SOURCE_FULL)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        assert not (await client.call("get_cited_element", {
            "source_id": mounted["source_id"],
            "element_id": mounted["element_id"],
        })).isError
        for tool in ("get_source_status", "reparse_source", "delete_source"):
            assert (await client.call(
                tool, {"source_id": mounted["source_id"]}
            )).isError, f"{tool} 不得跨参考库管理来源"
            assert (await client.call(
                tool, {"source_id": synthetic["source_id"]}
            )).isError, f"{tool} 不得触碰隐藏合成源"
            assert (await client.call(
                tool, {"source_id": "src-does-not-exist"}
            )).isError

    assert _source_row_exists(repo, mounted["source_id"])
    assert _source_row_exists(repo, synthetic["source_id"])


@pytest.mark.anyio
async def test_reparse_source_queues_one_job_and_refuses_a_source_being_parsed(
    mcp_env, scheduled_jobs
):
    """Re-parse has no single-flight guard behind it — the browser route
    submits unconditionally — so calling it in a loop would queue N full
    pipelines for one document. The tool probes the ingestion service's own
    per-source chunk lock, the same mutex ``process_source`` holds from
    ``replace_elements`` through ``build_chunks``."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = _seed_cited_source(repo, notebook_id, "reparse-target")
    ingestion = repo._runtime.source_ingestion

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_WRITE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call(
            "reparse_source", {"source_id": seeded["source_id"]}
        ))
        assert payload == {
            "source_id": seeded["source_id"],
            "queued": True,
            "truncation": payload["truncation"],
        }
        _assert_budgeted(payload)
        assert [args for _fn, args in scheduled_jobs] == [(seeded["source_id"],)]

        # Hold the real lock from this thread; the tool body runs on an anyio
        # worker thread, so the contention is genuine.
        with ingestion.hold_source_chunk_lock(seeded["source_id"]):
            busy = await client.call(
                "reparse_source", {"source_id": seeded["source_id"]}
            )
        assert busy.isError
        assert "pars" in busy.content[0].text.lower()

    # The refused call queued nothing extra.
    assert len(scheduled_jobs) == 1


@pytest.mark.anyio
async def test_delete_source_removes_an_agent_source_but_never_a_user_one(
    mcp_env, scheduled_jobs
):
    """THE safety case for this whole surface.

    A source a person added must survive a delete_source call from a token
    that holds every source scope and owns the notebook. The only thing
    standing between the two outcomes is the v48 provenance check, so removing
    or inverting it turns this test red (mutation-verified; see task report).
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    human = _seed_cited_source(repo, notebook_id, "human-authored")
    token = _agent_token(mcp_env, _SOURCE_FULL)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        agent_source = _payload(await client.call("add_source_text", {
            "title": "Agent 写的", "content_md": "Agent 自己的产物。",
        }))["source_id"]

        refused = await client.call(
            "delete_source", {"source_id": human["source_id"]}
        )
        assert refused.isError
        assert "user" in refused.content[0].text.lower()
        assert _source_row_exists(repo, human["source_id"]), (
            "用户添加的来源必须原样留在库里"
        )

        deleted = _payload(await client.call(
            "delete_source", {"source_id": agent_source}
        ))
        assert deleted["deleted"] is True
        _assert_budgeted(deleted)
        assert not _source_row_exists(repo, agent_source)

        # Repeating it AFTER it completed is indistinguishable from deleting
        # something that never existed — the same no-disclosure shape every
        # other miss on this surface has. (Two genuinely IN-FLIGHT deletes are
        # a different case and are safe for a different reason: both get past
        # the existence read, and `delete_source`'s own
        # `source_exists_for_update_tx` makes the loser's write a no-op.)
        assert (await client.call(
            "delete_source", {"source_id": agent_source}
        )).isError


@pytest.mark.anyio
async def test_source_scopes_do_not_imply_one_another(
    mcp_env, scheduled_jobs, reachable_pdf_url
):
    """sources:write and sources:delete are separate capabilities, and
    knowledge:read alone opens neither.

    ⚠ Every refusal below is asserted on an argument that a LATER session —
    differing only in scope — accepts. That is what makes the assertion about
    the scope gate: a nonexistent source id or an unreachable URL would be
    refused by a build with no scope check at all, so such a case proves
    nothing. Hence the pre-seeded Agent-stamped source and the stubbed
    reachable URL.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    app = mcp_env["app"]
    seeded = _seed_cited_source(repo, notebook_id, "scope-probe")["source_id"]
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET agent_profile_id=? WHERE id=?",
            (mcp_env["profile_a"].id, seeded),
        )

    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, _agent_token(mcp_env, _SOURCE_READ), manage_lifespan=False
        ) as reader:
            _payload(await reader.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            # knowledge:read really does open the read tool on this very source,
            # so the four refusals that follow are not "this session can do
            # nothing here".
            assert not (await reader.call(
                "get_source_status", {"source_id": seeded}
            )).isError
            assert (await reader.call("add_source_text", {
                "title": "无权", "content_md": "无权",
            })).isError
            assert (await reader.call("add_source_file", {
                "file_name": "无权.pdf", "content_base64": "YQ==",
            })).isError
            assert (await reader.call(
                "add_source_url", {"url": reachable_pdf_url}
            )).isError
            assert (await reader.call(
                "reparse_source", {"source_id": seeded}
            )).isError
            assert (await reader.call(
                "delete_source", {"source_id": seeded}
            )).isError

        async with OfficialMcpClient(
            app, _agent_token(mcp_env, _SOURCE_WRITE), manage_lifespan=False
        ) as writer:
            _payload(await writer.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            agent_source = _payload(await writer.call("add_source_text", {
                "title": "可写", "content_md": "可写的正文",
            }))["source_id"]
            assert not (await writer.call("add_source_file", {
                "file_name": "可写.pdf", "content_base64": "YQ==",
            })).isError
            # The same URL and the same source id the reader was refused.
            assert not (await writer.call(
                "add_source_url", {"url": reachable_pdf_url}
            )).isError
            assert not (await writer.call(
                "reparse_source", {"source_id": seeded}
            )).isError
            assert (await writer.call(
                "delete_source", {"source_id": agent_source}
            )).isError, "sources:write 不蕴含 sources:delete"

        async with OfficialMcpClient(
            app, _agent_token(mcp_env, _SOURCE_DELETE), manage_lifespan=False
        ) as deleter:
            _payload(await deleter.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            assert (await deleter.call("add_source_text", {
                "title": "只许删", "content_md": "只许删",
            })).isError, "sources:delete 不蕴含 sources:write"
            assert (await deleter.call("add_source_file", {
                "file_name": "只许删.pdf", "content_base64": "YQ==",
            })).isError, "sources:delete 不蕴含 sources:write"
            assert (await deleter.call(
                "reparse_source", {"source_id": seeded}
            )).isError
            assert not (await deleter.call(
                "delete_source", {"source_id": agent_source}
            )).isError

    assert not _source_row_exists(repo, agent_source)
    assert _source_row_exists(repo, seeded)


@pytest.mark.anyio
async def test_delete_source_accepts_a_row_stamped_by_a_sibling_agent_profile(
    mcp_env, scheduled_jobs
):
    """The decided rule is "SOME Agent added this row", not "this profile did".

    Agent profiles are the owner's own bookkeeping — they get rotated and
    revoked — so a source left behind by a retired profile must not become
    permanently undeletable through this surface. Without this case, narrowing
    the criterion to ``agent_profile_id == principal.profile_id`` would turn no
    test red.

    The user-added protection is unaffected and is pinned separately by
    ``test_delete_source_removes_an_agent_source_but_never_a_user_one``.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    app = mcp_env["app"]

    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app,
            _agent_token(mcp_env, _SOURCE_WRITE, profile=mcp_env["profile_b"]),
            manage_lifespan=False,
        ) as codex:
            _payload(await codex.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            source_id = _payload(await codex.call("add_source_text", {
                "title": "Codex 建的", "content_md": "另一个 Agent 的产物。",
            }))["source_id"]
        assert _stored_provenance(repo, source_id) == mcp_env["profile_b"].id

        async with OfficialMcpClient(
            app,
            _agent_token(mcp_env, _SOURCE_DELETE, profile=mcp_env["profile_a"]),
            manage_lifespan=False,
        ) as claude_code:
            _payload(await claude_code.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            deleted = _payload(await claude_code.call(
                "delete_source", {"source_id": source_id}
            ))
            assert deleted["deleted"] is True

    assert not _source_row_exists(repo, source_id)


@pytest.mark.anyio
async def test_source_tools_require_a_selected_notebook(mcp_env, scheduled_jobs):
    """The gate every data tool here shares: no select_notebook, no tool."""
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_FULL)
    ) as client:
        assert (await client.call("add_source_text", {
            "title": "未选库", "content_md": "未选库",
        })).isError
        assert (await client.call("add_source_file", {
            "file_name": "未选库.pdf", "content_base64": "YQ==",
        })).isError
        assert (await client.call("add_source_url", {
            "url": "https://example.test/a.pdf",
        })).isError
        assert (await client.call(
            "get_source_status", {"source_id": "src-anything"}
        )).isError
        assert (await client.call(
            "reparse_source", {"source_id": "src-anything"}
        )).isError
        assert (await client.call(
            "delete_source", {"source_id": "src-anything"}
        )).isError


@pytest.mark.anyio
async def test_source_writes_are_refused_in_a_read_only_shared_notebook(
    mcp_env, scheduled_jobs
):
    """An allowlist entry proves the token may SELECT a notebook; it says
    nothing about writing to it.

    Bob is a read-only member of Alice's notebook — a share that gives him no
    way to add or delete a source in the browser. His Agent token carries both
    source scopes anyway, because ``require_agent_access`` only ever proves
    READ access (``user_can_read_notebook`` = owner ∪ member). Without the
    owner-only gate on top, this share would become writable through MCP.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    seeded = _seed_cited_source(repo, notebook_id, "readonly-share")
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET agent_profile_id=? WHERE id=?",
            (mcp_env["bob_profile"].id, seeded["source_id"]),
        )
    bob_token = _agent_token(mcp_env, _SOURCE_FULL, user="bob")

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        # Reading is exactly what a read-only member may do.
        assert not (await client.call(
            "get_source_status", {"source_id": seeded["source_id"]}
        )).isError
        assert (await client.call("add_source_text", {
            "title": "只读成员", "content_md": "只读成员写不进来",
        })).isError
        assert (await client.call("add_source_file", {
            "file_name": "只读成员.pdf", "content_base64": "YQ==",
        })).isError
        assert (await client.call(
            "reparse_source", {"source_id": seeded["source_id"]}
        )).isError
        # Agent-added is NOT sufficient: ownership is checked first, so even a
        # row this token's own profile stamped stays untouchable from here.
        assert (await client.call(
            "delete_source", {"source_id": seeded["source_id"]}
        )).isError

    assert _source_row_exists(repo, seeded["source_id"])
    assert len(repo.list_sources(notebook_id)) == 1
    assert scheduled_jobs == []


@pytest.mark.anyio
async def test_knowhow_code_write_is_allowed_in_a_read_only_shared_notebook(
    mcp_env,
):
    """The deliberate counterpart of the test above: the two Agent write
    surfaces use DIFFERENT authority models on purpose.

    Source management layers an owner-only gate on top of its scopes
    (``mcp_server._writable_notebook``) because adding, re-parsing, or
    deleting a document reaches every member's retrieval. ``knowhow:code``
    does not: design doc §⑥-4 keeps an Agent's write capability entirely
    scope-driven (``deps.user_or_agent_scope`` documents the same rule for
    the HTTP twins), and a cell code attachment is inert — never executed,
    indexed, embedded, or projected — so the blast-radius argument does not
    carry over. Recorded as a decision in AGENTS.md / CLAUDE.md /
    docs/product-and-api*.md; this test pins the knowhow side so the
    divergence cannot drift silently.

    Bob is a read-only member of Alice's notebook: the very share where his
    ``add_source_text`` is refused accepts his cell-code write.
    """
    table_id, row_id, method_id = _seed_knowhow_table(mcp_env["notebook"].id)
    bob_token = _agent_token(
        mcp_env, ["knowledge:read", "knowhow:code"], user="bob"
    )

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        put_result = _payload(await client.call(
            "put_knowhow_cell_code",
            {
                "row_id": row_id, "column_id": method_id,
                "code_text": "print('bob')", "language": "python",
            },
        ))
        assert put_result["status"] == "implemented"

        row = _payload(await client.call("get_knowhow_row", {"row_id": row_id}))
        assert row["code"][0]["column_id"] == method_id
        assert row["code"][0]["updated_by"] == mcp_env["bob_profile"].name


# --- build/maintenance tools -------------------------------------------
# "maintenance:execute" was already a valid AGENT_SCOPES entry (and offered by
# the token-creation UI) before build_kg/build_retrieval_index existed to
# consume it -- these tests cover that closing of the loop, plus the
# read-only get_build_status that sits on "knowledge:read" like the source
# tools' own get_source_status does.
_MAINTENANCE = ["knowledge:read", "maintenance:execute"]


@pytest.fixture
def scheduled_kg_jobs(monkeypatch):
    """Capture build_kg's background submission instead of running a real
    extraction pipeline (with real LLM calls) on a worker thread mid-test."""
    from app.services import background_jobs

    calls: list = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: calls.append((fn, args, kwargs)),
    )
    return calls


class _KgExtractStub:
    """Just enough for `configured("kg_extract")` to read True via
    bind_chat_client. `execute_notebook_kg_job` is never reached in these
    tests -- `scheduled_kg_jobs` stubs the submission that would run it --
    so an actual call here is a test bug, not a feature path."""

    configured = True

    def chat_json(self, *args, **kwargs):
        raise AssertionError(
            "kg_extract must not be invoked: background_jobs.submit is "
            "stubbed by the scheduled_kg_jobs fixture in this test"
        )


def _mark_notebook_base_tier(repo, notebook_id: str) -> None:
    """The cheapest eligible-for-a-retrieval-index notebook shape (mirrors
    test_scale_index_repo.py's own `test_trigger_when_and_mode`):
    `eligible()` returns True immediately for tier=='base', before it ever
    looks at chunk counts or copy stats -- no source/chunk/embedding seeding
    needed at all."""
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook_id,))


@pytest.mark.anyio
async def test_build_kg_refuses_when_no_chat_model_is_configured(mcp_env):
    """Default mcp_env clears every chat/embedding provider env var, so this
    reproduces a deployment with no LLM configured -- the same state
    kg_routes.build_kg's own 409 guards against -- without any extra setup.
    The message must not name the deployment's env vars (mirrors
    add_source_url's MinerUCloudNotConfigured wording)."""
    notebook_id = mcp_env["notebook"].id
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        refused = await client.call("build_kg", {})
        assert refused.isError
        text = refused.content[0].text
        assert "chat model" in text.lower()
        assert "MINERU" not in text and "OPENAI" not in text.upper()


@pytest.mark.anyio
async def test_build_kg_queues_one_job_and_refuses_a_concurrent_second_call(
    mcp_env, scheduled_kg_jobs
):
    """Happy path with exact values, plus the durable per-notebook
    single-flight (kg_build_jobs' own conditional unique index) surfacing as
    the router's own Chinese busy sentence -- 409 semantics, not an error the
    caller should retry differently; poll get_build_status instead."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bind_chat_client(repo, "kg_extract", _KgExtractStub())

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        started = _payload(await client.call("build_kg", {}))
        assert started["mode"] == "incremental"
        assert started["status"] == "building"
        assert started["job_id"]
        _assert_budgeted(started)

        # P3-3: the poll chain an Agent actually follows -- build_kg's own
        # response is a point-in-time echo, get_build_status is the thing
        # meant to be re-read afterward. `prepare_notebook_kg_job` marks the
        # in-process `kg_building` flag synchronously (before
        # background_jobs.submit is even reached), so this is visible without
        # the stubbed submission ever running.
        polled = _payload(await client.call("get_build_status", {}))
        assert polled["kg"]["building"] is True
        assert polled["kg"]["job"] is not None
        assert polled["kg"]["job"]["job_id"] == started["job_id"]
        _assert_budgeted(polled)

        busy = await client.call("build_kg", {})
        assert busy.isError
        assert "当前笔记本已有知识图谱分析任务正在运行" in busy.content[0].text

    assert len(scheduled_kg_jobs) == 1
    fn, args, kwargs = scheduled_kg_jobs[0]
    assert fn == repo.execute_notebook_kg_job
    assert args == (notebook_id, started["job_id"], "incremental")
    assert kwargs == {
        "retry_partial": True,
        "name": f"buildkg-{notebook_id}",
        "notify_pending": True,
    }


@pytest.mark.anyio
async def test_build_retrieval_index_validates_when_before_touching_an_eligible_notebook(
    mcp_env
):
    """`when` outside {now, idle} must be refused BEFORE the repository is
    even called -- proven on a base-tier ELIGIBLE notebook, not the default
    small one.

    P3-2: on the (ineligible-by-construction) default notebook, this class of
    guard is indirectly proven at best -- deleting the `when` validation
    entirely still produces an `isError` response, because the fallen-
    through call reaches `trigger_scale_index_rebuild` and dies on
    eligibility instead ("notebook too small..."), which happens to not
    contain the word "when" either. That coincidence is not what this test
    is supposed to be pinning. On an ELIGIBLE notebook the two failure modes
    are observably different: a deleted check lets "soon" fall through the
    `when == "idle"` branch as an implicit "now" and actually START A BUILD
    (or return "already_building"), which this asserts did NOT happen, on
    top of the error text actually saying so."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    _mark_notebook_base_tier(repo, notebook_id)

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        bad_when = await client.call("build_retrieval_index", {"when": "soon"})
        assert bad_when.isError
        assert "when" in bad_when.content[0].text.lower()

    # No immediate-build side effect: neither queued nor (would-be
    # synchronous) building/already_building.
    assert notebook_id not in repo._runtime.scale_artifacts.idle_queue
    assert notebook_id not in repo._runtime.scale_artifacts.building


@pytest.mark.anyio
async def test_build_retrieval_index_lets_ineligibility_through(mcp_env):
    """A fresh mcp_env notebook (personal tier, no index, few chunks) is
    ineligible by construction -- the service layer's own readable message
    ("notebook too small and not base-tier...") comes through unedited,
    exactly like add_source_url lets add_url_sources' rejection wording
    through."""
    notebook_id = mcp_env["notebook"].id
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        ineligible = await client.call("build_retrieval_index", {"when": "now"})
        assert ineligible.isError
        assert "too small" in ineligible.content[0].text.lower()


@pytest.mark.anyio
async def test_build_retrieval_index_queues_for_the_low_traffic_window(mcp_env):
    """when='idle' on an eligible (base-tier) notebook queues instead of
    starting immediately -- exercising the one branch of `trigger()` that
    never spawns real indexing work, so this needs no embedder stub.

    Also covers the get_build_status read-back an Agent actually polls with
    (P2-1's mutation-guarded scale_index key-priority fix): queue_position
    AND total_chunks/unindexed_sources must all be in the SAME response --
    before the fix, alphabetical field-limit truncation silently dropped the
    latter two behind higher-priority-but-lower-value keys.

    Cleans up the queue entry afterward (mirrors
    test_scale_index_repo.py::test_trigger_when_and_mode's own drain): a
    dangling `idle_queue` entry left on this test's SQLiteRepository instance
    would be a live target for the runtime idle-queue scheduler thread the
    'idle' branch starts, which polls for the deployment's offpeak window
    (02:00-06:00, overlapping check_extended.sh's own 02:17 nightly run) and
    would build against a tmp DB this test has already torn down."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    _mark_notebook_base_tier(repo, notebook_id)

    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        queued = _payload(await client.call(
            "build_retrieval_index", {"when": "idle"}
        ))
        assert queued == {
            "status": "queued", "notebook_id": notebook_id,
            "truncation": queued["truncation"],
        }
        _assert_budgeted(queued)

        status = _payload(await client.call("get_build_status", {}))
        scale_index = status["scale_index"]
        assert scale_index["state"] == "queued"
        assert "queue_position" in scale_index
        assert "total_chunks" in scale_index
        assert "unindexed_sources" in scale_index
        _assert_budgeted(status)

    cancelled = repo.cancel_scale_index(notebook_id)
    assert cancelled["cancelled"] is True
    assert notebook_id not in repo._runtime.scale_artifacts.idle_queue


@pytest.mark.anyio
async def test_get_build_status_reports_kg_and_scale_index_state(mcp_env):
    """The one read behind both build tools: kg (ready/building/pending
    sources/current job) and scale_index (exists/building/eligible/state)
    sub-objects both present in a single call, readable by a plain
    knowledge:read token."""
    notebook_id = mcp_env["notebook"].id
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _SOURCE_READ)
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        status = _payload(await client.call("get_build_status", {}))
        assert status["kg"]["ready"] is False
        assert status["kg"]["building"] is False
        assert status["kg"]["pending_sources"] == 0
        assert status["kg"]["job"] is None
        assert status["scale_index"]["exists"] is False
        assert status["scale_index"]["eligible"] is False
        _assert_budgeted(status)


@pytest.mark.anyio
async def test_maintenance_tools_require_a_selected_notebook(mcp_env):
    async with OfficialMcpClient(
        mcp_env["app"], _agent_token(mcp_env, _MAINTENANCE)
    ) as client:
        assert (await client.call("build_kg", {})).isError
        assert (await client.call("build_retrieval_index", {})).isError
        assert (await client.call("get_build_status", {})).isError


@pytest.mark.anyio
async def test_maintenance_and_source_scopes_do_not_imply_one_another(
    mcp_env, scheduled_kg_jobs
):
    """Scope gates prove nothing about a build that would have failed
    anyway, so this configures kg_extract (a call WOULD succeed) and still
    shows the unscoped token refused -- knowledge:read alone reads
    get_build_status but opens neither write tool, and maintenance:execute
    does not imply sources:write either."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bind_chat_client(repo, "kg_extract", _KgExtractStub())
    app = mcp_env["app"]

    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, _agent_token(mcp_env, _SOURCE_WRITE), manage_lifespan=False
        ) as writer:
            _payload(await writer.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            # knowledge:read really does open the read tool here, so the two
            # refusals below are not "this session can do nothing".
            assert not (await writer.call("get_build_status", {})).isError
            assert (await writer.call("build_kg", {})).isError
            assert (await writer.call("build_retrieval_index", {})).isError

        async with OfficialMcpClient(
            app, _agent_token(mcp_env, _MAINTENANCE), manage_lifespan=False
        ) as maintainer:
            _payload(await maintainer.call(
                "select_notebook", {"notebook_id": notebook_id}
            ))
            assert (await maintainer.call("add_source_text", {
                "title": "无权", "content_md": "无权",
            })).isError, "maintenance:execute 不蕴含 sources:write"
            assert not (await maintainer.call("build_kg", {})).isError

    assert len(scheduled_kg_jobs) == 1


@pytest.mark.anyio
async def test_maintenance_writes_are_refused_in_a_read_only_shared_notebook(
    mcp_env, scheduled_kg_jobs
):
    """Same rule as the source-management tools: an allowlist entry proves a
    token may SELECT a notebook, not that it may write to it. Bob is a
    read-only member of Alice's notebook; his token carries maintenance:
    execute anyway, and only the owner-only gate stops a background build
    from starting through a share the browser itself renders read-only.

    Both underlying calls are made to WOULD-SUCCEED (kg_extract configured;
    notebook marked base-tier so the retrieval-index build is eligible) --
    otherwise a domain-level refusal (LLM not configured / notebook too
    small) would make `.isError` true for the wrong reason and this test
    would not actually be pinning the owner-only gate.

    P1 mutation guard (verified manually, then reverted -- see task report):
    swapping build_kg's `_writable_notebook` for `_selected_notebook` turns
    this red, because Bob IS a read-only member (`user_can_read_notebook`
    passes) and kg_extract is configured, so the call goes on to actually
    queue a job instead of being refused."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bind_chat_client(repo, "kg_extract", _KgExtractStub())
    _mark_notebook_base_tier(repo, notebook_id)
    bob_token = _agent_token(mcp_env, _MAINTENANCE, user="bob")

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        assert not (await client.call("get_build_status", {})).isError
        assert (await client.call("build_kg", {})).isError
        assert (await client.call("build_retrieval_index", {})).isError

    assert scheduled_kg_jobs == []


# --- Long-running tools: MCP progress heartbeat -----------------------------
#
# An MCP client does not wait forever. Claude Code aborts a tool call that has
# sent neither a response nor a progress notification for N seconds, and
# `ask_notebook` in `reasoning` mode runs for minutes -- so without a heartbeat
# the client gives up on a call the server is still executing successfully.
#
# The trap these tests exist for is that the fix is TWO things, and either one
# alone is silently useless: `_run_with_progress` must emit the beats, and the
# transport must be in SSE mode to carry them. With `json_response=True` the
# SDK opens a per-request stream, drains it looking for the response, and
# discards every notification on the way -- `report_progress` still "succeeds"
# and the client receives nothing. Only an end-to-end assertion catches that,
# which is why these go through the official client rather than spying on the
# server-side call.


def _slow_ask(mcp_env, monkeypatch, seconds: float):
    """Point `ask_notebook` at an answer that takes `seconds` to produce."""
    import time as _time

    service = mcp_env["service"]
    monkeypatch.setattr(
        service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env)
    )

    def slow_answer(*args, **kwargs):
        _time.sleep(seconds)
        return SimpleNamespace(
            answer_id="ans-slow", answer="Slow answer.", conclusion="Slow.",
            grounded=True, evidence_level="grounded", mode="chunk",
            conversation_id="conv-slow", anchors=[], citations=[],
        )

    monkeypatch.setattr(service, "ask", slow_answer)


@pytest.mark.anyio
async def test_a_slow_tool_heartbeats_progress_to_the_client(mcp_env, monkeypatch):
    """The whole point: beats reach the CLIENT while the tool is still running.

    Mutation guards (all three verified by replay, see task report):
      1. `json_response=True` on the FastMCP constructor -> zero beats
      2. deleting the `_run_with_progress` wrapper at ask_notebook's call site
         (back to a bare `anyio.to_thread.run_sync`) -> zero beats
      3. moving the heartbeat's `report_progress` after the work -> zero beats
    """
    from app.api.mcp_tools import _shared

    monkeypatch.setattr(_shared, "PROGRESS_HEARTBEAT_SECONDS", 0.05)
    _slow_ask(mcp_env, monkeypatch, 0.45)

    beats: list[tuple[float, float, str | None]] = []

    async def on_progress(progress, total, message):
        beats.append((asyncio.get_running_loop().time(), progress, message))

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        answer = _payload(await client.call(
            "ask_notebook", {"question": "What evidence exists?", "mode": "chunk"},
            progress_callback=on_progress,
        ))
        finished_at = asyncio.get_running_loop().time()

    assert answer["answer"] == "Slow answer."
    # A 450ms call at a 50ms interval; assert only that SOME arrived, so the
    # test does not become a scheduler-timing flake.
    assert len(beats) >= 2, beats
    # Every beat landed before the result, which is the property that keeps the
    # client's idle timer from expiring.
    assert all(at < finished_at for at, _, _ in beats)
    # MCP requires the progress value to increase with every notification.
    values = [progress for _, progress, _ in beats]
    assert values == sorted(values) and len(set(values)) == len(values), values
    # The message names the tool and the elapsed seconds and NOTHING else --
    # no question text, no notebook or source name. Same rule the telemetry
    # events follow, and this string goes to a client we do not control.
    assert all(message.startswith("ask_notebook: ") for _, _, message in beats)
    assert all("evidence" not in message for _, _, message in beats)


@pytest.mark.anyio
async def test_no_progress_is_sent_when_the_client_did_not_ask_for_it(
    mcp_env, monkeypatch
):
    """A client that sends no progressToken is charged nothing.

    `Context.report_progress` short-circuits before touching the session, so
    the heartbeat costs a sleeping task and no wire traffic. Spied on the
    SERVER side because there is, by construction, nothing to observe on the
    client side.
    """
    from mcp.server.session import ServerSession

    from app.api.mcp_tools import _shared

    sent: list[tuple] = []
    original = ServerSession.send_progress_notification

    async def spy(self, *args, **kwargs):
        sent.append((args, kwargs))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ServerSession, "send_progress_notification", spy)
    monkeypatch.setattr(_shared, "PROGRESS_HEARTBEAT_SECONDS", 0.05)
    _slow_ask(mcp_env, monkeypatch, 0.3)

    async def on_progress(progress, total, message):
        return None

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        answer = _payload(await client.call(
            "ask_notebook", {"question": "What evidence exists?", "mode": "chunk"}
        ))
        silent = list(sent)
        # Both directions in one test on purpose: asserting only "nothing was
        # sent" would still pass with the heartbeat deleted outright, which is
        # not the property being pinned.
        _payload(await client.call(
            "ask_notebook", {"question": "What evidence exists?", "mode": "chunk"},
            progress_callback=on_progress,
        ))

    assert answer["answer"] == "Slow answer."
    assert silent == []
    assert len(sent) >= 2, sent


@pytest.mark.anyio
async def test_a_tool_error_survives_the_heartbeat_task_group(mcp_env, monkeypatch):
    """The heartbeat must not turn a tool's own message into an ExceptionGroup.

    anyio 4 wraps an exception raised in a task group's BODY, and FastMCP turns
    whatever leaves a tool into the text the Agent reads. `_run_with_progress`
    therefore captures the failure in the child task and re-raises it after the
    group closes. Mutation guard: move the work back into the `async with`
    body and this reads `unhandled errors in a TaskGroup` instead.
    """
    from app.api.mcp_tools import _shared

    service = mcp_env["service"]
    monkeypatch.setattr(_shared, "PROGRESS_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(
        service, "get_notebook", lambda _id: _fake_notebook_summary(mcp_env)
    )

    def boom(*args, **kwargs):
        raise ValueError("this exact sentence must reach the Agent")

    monkeypatch.setattr(service, "ask", boom)

    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        failed = await client.call(
            "ask_notebook", {"question": "anything", "mode": "chunk"}
        )

    assert failed.isError
    text = failed.content[0].text
    assert "this exact sentence must reach the Agent" in text
    assert "TaskGroup" not in text and "ExceptionGroup" not in text


@pytest.mark.anyio
async def test_the_mcp_transport_answers_over_sse_not_buffered_json(mcp_env):
    """Pins the transport mode itself, in the one place it is observable.

    `json_response=True` would answer this POST `application/json` and quietly
    drop every notification the tools emit. Asserting the response's
    content-type keeps that flip from passing review as a harmless-looking
    one-word change.
    """
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        await _wait_for_ready(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {mcp_env['token_a'].token}"},
            follow_redirects=True,
        ) as http:
            accepted = await http.post(
                "/mcp",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "transport-mode", "version": "0"},
                    },
                },
            )
            # And the cost of SSE mode, stated as a test rather than left to be
            # discovered: a POST that accepts only JSON is refused. The
            # Streamable HTTP spec requires clients to send both media types.
            json_only = await http.post(
                "/mcp",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={
                    "jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "json-only", "version": "0"},
                    },
                },
            )

    assert accepted.status_code == 200
    assert accepted.headers["content-type"].startswith("text/event-stream")
    assert json_only.status_code == 406

# --- Agentic Memory P3 (T3): notebook understanding + observation log -----
# get_notebook_profile ("agent_profile:read") and add_observation
# ("agent_observation:write") -- the 21st and 22nd tools.
_PROFILE_READ = ["agent_profile:read"]
_OBSERVATION_WRITE = ["agent_observation:write"]


def _write_profile_block(
    repo, notebook_id: str, owner_id: str, label: str, *, value: str,
    evidence: list | None = None,
) -> None:
    """Seed one ``agent_notebook_profile`` row directly through the store --
    the SAME write path T6's manual "edit" and T4's consolidation job use.
    ``owner_id=""`` (mirrors ``agent_profile_job.BASE_CHAIN_OWNER``, not
    imported here to keep this test file's import list independent of that
    module's own import chain) is the shared base chain; any other owner id
    is that member's private overlay."""
    repo.agent_profile.write_block(
        notebook_id, owner_id, label,
        value=value, evidence=evidence or [], expected_revision=0,
        origin="user", actor=owner_id or "system",
    )


@pytest.mark.anyio
async def test_get_notebook_profile_returns_base_and_only_own_overlay(mcp_env):
    """Grouping is by each row's OWN ``owner_id`` column -- never by
    guessing which labels "belong" to which chain from their names.

    Alice ALSO has a row under a base-chain label (``corpus_shape``) written
    under her OWN overlay (``owner_id=alice.id``) -- an edge case a manual
    store-level edit could produce, since the label/scope pairing is only
    enforced by the HTTP route, not by the store itself. ``read_blocks``
    legitimately returns both this row and the real base row under the SAME
    label, because they are two distinct ``(notebook_id, owner_id, label)``
    rows: correct behaviour keeps them apart by ``owner_id``; label-based
    grouping cannot, and picks whichever one iterates last.

    Bob is a SEPARATE notebook member with his own overlay under the same
    label. His row is not even fetched by ``read_blocks(notebook_id,
    alice.id)`` (SQL: ``owner_id IN ('', ?)``) when Alice's token calls this
    tool, so its absence here is a store-level guarantee this test also
    exercises, though it is not what the mutation below targets.

    P3 mutation guard (verified manually with a temporary edit to
    ``_profile_projection``, then reverted -- see task report): switching
    the grouping from ``row["owner_id"] == owner_id`` to membership in
    ``BASE_LABELS``/``OVERLAY_LABELS`` turns this red -- ``rows`` (shared
    across both the "shared" and "mine" calls) then lets Alice's PRIVATE
    ``corpus_shape`` row win the ``by_label["corpus_shape"]`` slot in the
    "shared" projection too (SQL orders ``owner_id, label`` so her row
    sorts after the real base row), silently presenting her own private
    opinion as the notebook's shared consensus to any member who calls this
    tool -- not only to Alice herself.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    _write_profile_block(
        repo, notebook_id, "", "corpus_shape", value="Mostly PDFs about RF design."
    )
    _write_profile_block(
        repo, notebook_id, mcp_env["alice"].id, "corpus_shape",
        value="Alice's own private opinion -- must never be shown as shared.",
    )
    _write_profile_block(
        repo, notebook_id, mcp_env["alice"].id, "retrieval_notes",
        value="Prefer exact_lookup for part numbers.",
    )
    _write_profile_block(
        repo, notebook_id, mcp_env["bob"].id, "retrieval_notes",
        value="Bob's own private overlay note -- must never reach Alice.",
    )
    token = _agent_token(mcp_env, _PROFILE_READ)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call("get_notebook_profile", {}))

    assert payload["enabled"] is True
    assert payload["notebook_id"] == notebook_id
    assert payload["shared"] == [
        {
            "label": "corpus_shape",
            "value": "Mostly PDFs about RF design.",
            "updated_at": payload["shared"][0]["updated_at"],
        }
    ]
    mine_by_label = {item["label"]: item["value"] for item in payload["mine"]}
    assert mine_by_label == {
        "corpus_shape": "Alice's own private opinion -- must never be shown as shared.",
        "retrieval_notes": "Prefer exact_lookup for part numbers.",
    }
    assert "Bob's own private overlay" not in json.dumps(payload, ensure_ascii=False)
    assert payload["content_is_untrusted_evidence"] is True
    assert payload["citable"] is False


@pytest.mark.anyio
async def test_get_notebook_profile_never_leaks_evidence_source_ids(mcp_env):
    """``evidence``/``revision``/``updated_origin``/``history`` are a
    WHITELIST exclusion, not redaction after the fact -- a token holding
    only ``agent_profile:read`` (no ``knowledge:read``) must not be able to
    probe for source ids it otherwise has no way to enumerate."""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    _write_profile_block(
        repo, notebook_id, "", "key_entities", value="DAC08, ADC12 parts.",
        evidence=[{"source_id": "src-should-never-leak-9f21"}],
    )
    token = _agent_token(mcp_env, _PROFILE_READ)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        payload = _payload(await client.call("get_notebook_profile", {}))

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "src-should-never-leak-9f21" not in serialized
    assert "evidence" not in payload["shared"][0]
    assert set(payload["shared"][0].keys()) == {"label", "value", "updated_at"}


@pytest.mark.anyio
async def test_get_notebook_profile_reports_disabled_instead_of_failing(
    mcp_env, monkeypatch
):
    notebook_id = mcp_env["notebook"].id
    token = _agent_token(mcp_env, _PROFILE_READ)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
        get_settings.cache_clear()
        try:
            payload = _payload(await client.call("get_notebook_profile", {}))
        finally:
            monkeypatch.setenv("AGENT_PROFILE_ENABLED", "true")
            get_settings.cache_clear()

    payload.pop("truncation")
    assert payload == {
        "notebook_id": notebook_id, "enabled": False, "shared": [], "mine": [],
    }


@pytest.mark.anyio
async def test_add_observation_fails_loudly_when_disabled(mcp_env, monkeypatch):
    """The deliberate counterpart of
    ``test_get_notebook_profile_reports_disabled_instead_of_failing`` right
    above: the READ side degrades to ``enabled: False`` because a caller
    cannot act on a closed sign, but the WRITE side must not go on quietly
    accumulating rows a now-disabled consolidation pass will never read (see
    ``add_observation``'s own kill-switch comment — "a 5th ... but a
    DIFFERENT contract on purpose"). ``isError`` here, not a soft
    ``{"accepted": False}`` payload."""
    notebook_id = mcp_env["notebook"].id
    token = _agent_token(mcp_env, _OBSERVATION_WRITE)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
        get_settings.cache_clear()
        try:
            result = await client.call("add_observation", {
                "text": "Should never be written while disabled.",
                "client_request_id": "obs-disabled-1",
            })
        finally:
            monkeypatch.setenv("AGENT_PROFILE_ENABLED", "true")
            get_settings.cache_clear()

    assert result.isError
    repo = repository()
    assert repo.agent_observations.list_observations(
        notebook_id, mcp_env["alice"].id, limit=10
    ) == []


@pytest.mark.anyio
async def test_add_observation_is_idempotent_per_client_request_id(mcp_env):
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    token = _agent_token(mcp_env, _OBSERVATION_WRITE)

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        first = _payload(await client.call("add_observation", {
            "text": "Tried exact_lookup for part numbers; worked well.",
            "client_request_id": "obs-1",
        }))
        second = _payload(await client.call("add_observation", {
            "text": "A different body -- must be ignored on replay.",
            "client_request_id": "obs-1",
        }))

    assert first["accepted"] is True
    assert first["deduplicated"] is False
    assert second["observation_id"] == first["observation_id"]
    assert second["deduplicated"] is True
    rows = repo.agent_observations.list_observations(
        notebook_id, mcp_env["alice"].id, limit=10
    )
    assert len(rows) == 1
    assert rows[0]["text"] == "Tried exact_lookup for part numbers; worked well."


@pytest.mark.anyio
async def test_add_observation_is_allowed_for_a_read_only_shared_notebook(mcp_env):
    """The deliberate counterpart of
    ``test_source_writes_are_refused_in_a_read_only_shared_notebook``: the
    two Agent write surfaces use DIFFERENT authority models on purpose (see
    ``mcp_server._writable_notebook``'s own docstring, point 2 of its "two
    writes" section). Bob is a read-only member of Alice's notebook -- the
    very share where his ``add_source_text`` is refused accepts his
    observation write, into HIS OWN overlay only.

    P3 mutation guard (verified manually, then reverted -- see task report):
    swapping ``add_observation``'s ``_selected_notebook`` for
    ``_writable_notebook`` turns this red, because Bob is not the
    notebook's owner.
    """
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bob_token = _agent_token(mcp_env, _OBSERVATION_WRITE, user="bob")

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        result = _payload(await client.call("add_observation", {
            "text": "Bob's own usage note.",
            "client_request_id": "bob-obs-1",
        }))

    assert result["accepted"] is True
    rows = repo.agent_observations.list_observations(
        notebook_id, mcp_env["bob"].id, limit=10
    )
    assert len(rows) == 1
    assert rows[0]["agent_profile_id"] == mcp_env["bob_profile"].id
    # Never lands in Alice's own scope -- owner_id is baked into the read
    # predicate, not inferred client-side.
    assert repo.agent_observations.list_observations(
        notebook_id, mcp_env["alice"].id, limit=10
    ) == []


@pytest.mark.anyio
async def test_add_observation_landing_after_member_removal_is_compensated(
    mcp_env, monkeypatch,
):
    """codex #535 R11 P2:访问校验与 append 是两步——成员在中间被移出时,移除
    路径先清了他的行、append 随后落地,留下重新加入即复活的孤儿行。修复是
    append 之后**复核访问**:撤销即补偿清理并报错。这里用 append 包装器把
    「移除恰落在两步之间」确定性排出来(真实并发无法稳定复现)。"""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bob = mcp_env["bob"]
    bob_token = _agent_token(mcp_env, _OBSERVATION_WRITE, user="bob")

    store = repo.agent_observations
    real_append = store.append_observation

    def append_then_remove(*args, **kwargs):
        result = real_append(*args, **kwargs)
        # 模拟移除恰好在 append 与复核之间完成:清行 + 撤成员
        repo.remove_member(notebook_id, bob.id)
        return result

    monkeypatch.setattr(store, "append_observation", append_then_remove)

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        result = await client.call("add_observation", {
            "text": "landed after removal", "client_request_id": "race-1",
        })

    assert result.isError
    assert repo.agent_observations.list_observations(
        notebook_id, bob.id, limit=10
    ) == [], "移除后落地的观察必须被补偿清理,不得留下复活孤儿行"


@pytest.mark.anyio
async def test_token_level_failure_after_append_keeps_the_rows(
    mcp_env, monkeypatch,
):
    """codex #535 R13 P2:append 后的复核只认 notebook 读权——token 在两步
    之间被吊销/过期不清行:owner 仍是成员,行是他自己的合法队列,按 token
    失效补偿会把他保留的全部观察(含别的 Agent 写的)一起误删。"""
    repo = repository()
    notebook_id = mcp_env["notebook"].id
    bob = mcp_env["bob"]

    issued = mcp_env["service"].issue_agent_token(
        bob.id, mcp_env["bob_profile"].id, _OBSERVATION_WRITE,
        notebook_id, [notebook_id], None,
    )
    bob_token = issued.token
    store = repo.agent_observations
    real_append = store.append_observation

    def append_then_revoke_token(*args, **kwargs):
        result = real_append(*args, **kwargs)
        # token 失效,notebook 读权仍在——复核只认读权,不得触发补偿
        repo.revoke_agent_token(bob.id, issued.id)
        return result

    monkeypatch.setattr(store, "append_observation", append_then_revoke_token)

    async with OfficialMcpClient(mcp_env["app"], bob_token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        result = _payload(await client.call("add_observation", {
            "text": "written just before revocation", "client_request_id": "tok-1",
        }))

    assert result["accepted"] is True
    rows = repo.agent_observations.list_observations(
        notebook_id, bob.id, limit=10
    )
    assert len(rows) == 1, "token 级失效不得触发补偿清理"


@pytest.mark.anyio
async def test_add_observation_requires_its_own_scope(mcp_env):
    notebook_id = mcp_env["notebook"].id
    token = _agent_token(mcp_env, ["knowledge:read"])

    async with OfficialMcpClient(mcp_env["app"], token) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        assert (await client.call("add_observation", {
            "text": "No scope for this.", "client_request_id": "no-scope-1",
        })).isError
