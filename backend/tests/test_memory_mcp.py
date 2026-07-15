from __future__ import annotations

import json
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
from app.core.config import get_settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import MemoryHit, NotebookCreate


PUBLIC_TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "get_memory",
    "ask_notebook",
    "propose_memory",
}
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

    async def call(self, name: str, arguments: dict | None = None):
        assert self.session is not None
        return await self.session.call_tool(name, arguments or {})


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mcp.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
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
            anchors=[
                SimpleNamespace(
                    key=f"k_{index}",
                    object_id=f"object-{index}",
                    object_type="custom-" + ("t" * 1_000),
                    label=sentinel * 500,
                    source_title=sentinel * 500,
                    location_label=sentinel * 500,
                    tier="personal",
                    provenance={"nested": [sentinel * 100 for _ in range(100)]},
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

    assert set(responses) == PUBLIC_TOOLS
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
    assert _json_chars(ask["anchors"]) <= 3_500
    assert ask["anchors"]
    assert all(_json_chars(anchor["provenance"]) <= 500 for anchor in ask["anchors"])
    assert ask["anchors"][0]["key"] == "k_0"
    assert ask["anchors"][0]["object_id"] == "object-0"
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
    from app.services.memory_inputs import (
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
