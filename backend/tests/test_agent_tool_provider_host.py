from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from types import SimpleNamespace

import httpx
import pytest

from app.api.mcp_server import (
    CORE_TOOLS,
    PUBLIC_TOOLS,
    create_memory_mcp,
    mcp_public_tools,
)
from app.domain.agent_tools import (
    AGENT_SCOPES,
    AGENT_TOOL_SCOPE_POLICIES,
    AgentExecutionContext,
    AgentToolAccessPolicy,
    AgentToolParameter,
    AgentToolValueKind,
)
from app.extension_sdk import (
    AGENT_TOOL_PROVIDER_POINT,
    EXTENSION_API_VERSION,
    AgentToolDescriptor,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extensions import build_extension_runtime, default_extension_runtime
from app.extensions.registry import ExtensionRegistryError
from app.core.event_logging import EventLogger
from app.models.identity import AgentPrincipal
from tests.test_memory_mcp import OfficialMcpClient, _payload, mcp_env


PLUGIN_ID = "builtin.agent_fixture"
CONTRIBUTION_ID = "builtin.agent_fixture.tools"


class _Provider:
    def __init__(self, descriptors: tuple[AgentToolDescriptor, ...]) -> None:
        self.descriptors = descriptors
        self.calls: list[tuple[object, ...]] = []

    def tools(self) -> tuple[AgentToolDescriptor, ...]:
        self.calls.append(("tools",))
        return self.descriptors

    def invoke(self, tool_name, context, arguments):
        self.calls.append(("invoke", tool_name, context, arguments))
        return {"echo": arguments.get("message", ""), "count": len(arguments)}


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar) -> None:
        registrar.add_contributor(self.contribution)


def _descriptor(
    name: str = "plugin_echo",
    *,
    scope: str = "knowledge:read",
    policy: AgentToolAccessPolicy = AgentToolAccessPolicy.READ,
) -> AgentToolDescriptor:
    return AgentToolDescriptor(
        name=name,
        description="Echo a bounded value through a trusted test provider.",
        required_scope=scope,
        access_policy=policy,
        parameters=(
            AgentToolParameter("message", AgentToolValueKind.STRING),
            AgentToolParameter("count", AgentToolValueKind.INTEGER),
        ),
    )


def _bundle(
    provider: object,
    *,
    kind=ContributionKind.CONTRIBUTOR,
    availability=None,
) -> _Bundle:
    declaration = ContributionDeclaration(
        CONTRIBUTION_ID,
        AGENT_TOOL_PROVIDER_POINT,
        kind,
    )
    return _Bundle(
        ExtensionManifest(
            id=PLUGIN_ID,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="Agent tool fixture",
            trust="builtin",
            contributions=(declaration,),
        ),
        ExtensionContribution(declaration, provider, availability),
    )


def _runtime(provider: _Provider, *, availability=None):
    return build_extension_runtime(
        (_bundle(provider, availability=availability),),
        trusted_agent_tool_plugins=frozenset({PLUGIN_ID}),
    )


def test_provider_requires_explicit_core_trust_and_contributor_kind() -> None:
    provider = _Provider((_descriptor(),))
    with pytest.raises(ExtensionRegistryError, match="explicitly trusted"):
        build_extension_runtime((_bundle(provider),))
    with pytest.raises(ExtensionRegistryError, match="contributor"):
        build_extension_runtime(
            (_bundle(provider, kind=ContributionKind.PROVIDER),),
            trusted_agent_tool_plugins=frozenset({PLUGIN_ID}),
        )


def test_scope_policy_is_core_owned_and_unknown_scopes_are_rejected() -> None:
    with pytest.raises(ExtensionRegistryError, match="access policy"):
        _runtime(
            _Provider(
                (
                    _descriptor(
                        scope="sources:write",
                        policy=AgentToolAccessPolicy.READ,
                    ),
                )
            )
        )
    with pytest.raises(ExtensionRegistryError, match="unknown agent tool scope"):
        _runtime(_Provider((_descriptor(scope="invented:scope"),)))


def test_provider_catalog_and_context_are_frozen_and_minimal() -> None:
    provider = _Provider((_descriptor(),))
    host = _runtime(provider).agent_tools
    assert len(host.public_tools) == 1
    tool = host.public_tools[0]
    assert host.available(tool) is True
    context = AgentExecutionContext("actor-1", "notebook-1", PLUGIN_ID, tool.name)
    arguments = MappingProxyType({"message": "hello", "count": 2})
    assert host.invoke(tool, context, arguments) == {"echo": "hello", "count": 2}
    _, _, seen_context, seen_arguments = provider.calls[-1]
    assert seen_context is context
    assert seen_arguments is arguments
    assert not hasattr(context, "repository")
    assert not hasattr(context, "token")
    assert not hasattr(context, "server")


def test_provider_availability_is_live_without_changing_the_public_catalog() -> None:
    state = {"available": False}

    def availability(context):
        assert context.tool_name == "plugin_echo"
        return (
            Availability.available()
            if state["available"]
            else Availability(AvailabilityStatus.UNAVAILABLE, "fixture_disabled")
        )

    host = _runtime(_Provider((_descriptor(),)), availability=availability).agent_tools
    tool = host.public_tools[0]
    assert host.available(tool) is False
    state["available"] = True
    assert host.available(tool) is True
    assert host.public_tools == (tool,)


def test_catalog_freezes_the_bound_handler_and_scope_policy_table() -> None:
    provider = _Provider((_descriptor(),))
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    provider.invoke = lambda *_args: {"replaced": True}  # type: ignore[method-assign]
    result = host.invoke(
        tool,
        AgentExecutionContext("actor", "notebook", PLUGIN_ID, tool.name),
        MappingProxyType({"message": "frozen", "count": 1}),
    )
    assert result == {"echo": "frozen", "count": 2}
    assert frozenset(AGENT_TOOL_SCOPE_POLICIES) == AGENT_SCOPES
    with pytest.raises(TypeError):
        AGENT_TOOL_SCOPE_POLICIES["invented:write"] = (  # type: ignore[index]
            AgentToolAccessPolicy.READ
        )


@pytest.mark.anyio
async def test_combined_catalog_keeps_core_prefix_and_plugin_schema() -> None:
    provider = _Provider((_descriptor(),))
    host = _runtime(provider).agent_tools

    def poison_provider():
        raise AssertionError("tool discovery must not resolve the repository")

    server, _app = create_memory_mcp(
        poison_provider,
        agent_tool_provider_host=host,
    )
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == (*CORE_TOOLS, "plugin_echo")
    assert mcp_public_tools(server) == (*CORE_TOOLS, "plugin_echo")
    plugin = tools[-1]
    assert plugin.inputSchema["required"] == ["message", "count"]
    assert plugin.inputSchema["properties"]["message"]["type"] == "string"
    assert plugin.inputSchema["properties"]["count"]["type"] == "integer"
    assert provider.calls == [("tools",)]


def test_provider_tool_cannot_collide_with_the_core_catalog() -> None:
    host = _runtime(_Provider((_descriptor("list_notebooks"),))).agent_tools
    with pytest.raises(RuntimeError, match="duplicate Agent tool name"):
        create_memory_mcp(lambda: None, agent_tool_provider_host=host)


def test_invalid_provider_results_do_not_cross_the_host_boundary() -> None:
    provider = _Provider((_descriptor(),))
    provider.invoke = lambda *_args: {"bad": float("nan")}  # type: ignore[method-assign]
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    with pytest.raises(RuntimeError, match="invalid_agent_tool_result"):
        host.invoke(
            tool,
            AgentExecutionContext("actor", "notebook", PLUGIN_ID, tool.name),
            MappingProxyType({"message": "hello", "count": 1}),
        )


def test_default_public_tools_derive_from_the_default_frozen_runtime() -> None:
    assert PUBLIC_TOOLS == (
        *CORE_TOOLS,
        *(tool.name for tool in default_extension_runtime().agent_tools.public_tools),
    )


def test_provider_failure_is_mapped_without_leaking_its_message() -> None:
    provider = _Provider((_descriptor(),))
    failure = ValueError("safe provider failure")

    def fail(*_args):
        raise failure

    provider.invoke = fail  # type: ignore[method-assign]
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    with pytest.raises(RuntimeError, match="^agent_tool_failed$") as raised:
        host.invoke(
            tool,
            AgentExecutionContext("actor", "notebook", PLUGIN_ID, tool.name),
            MappingProxyType({"message": "hello", "count": 1}),
        )
    assert raised.value is not failure
    assert "safe provider failure" not in str(raised.value)


@pytest.mark.parametrize(
    "result",
    (
        {"items": [None] * 20_000},
        {"text": "x" * 1_000_000},
        {str(index): None for index in range(20_000)},
    ),
)
def test_provider_result_budget_rejects_huge_shapes_before_copying(result) -> None:
    provider = _Provider((_descriptor(),))
    provider.invoke = lambda *_args: result  # type: ignore[method-assign]
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    with pytest.raises(RuntimeError, match="invalid_agent_tool_result"):
        host.invoke(
            tool,
            AgentExecutionContext("actor", "notebook", PLUGIN_ID, tool.name),
            MappingProxyType({"message": "hello", "count": 1}),
        )


@pytest.mark.anyio
async def test_provider_adapter_emits_content_free_core_owned_audit(
    monkeypatch,
) -> None:
    from app.api import mcp_tool_host

    provider = _Provider((_descriptor(),))
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    events: list[dict[str, object]] = []
    audit_principal = AgentPrincipal(
        profile_id="profile-audit",
        profile_name="Audit profile",
        owner_id="user-audit",
        scopes=["knowledge:read"],
        default_notebook_id="notebook-1",
        notebook_ids=["notebook-1"],
        token_id="token-audit",
    )
    monkeypatch.setattr(mcp_tool_host, "_principal", lambda: audit_principal)

    monkeypatch.setattr(
        mcp_tool_host,
        "_selected_notebook",
        lambda *_args: (
            SimpleNamespace(owner_id="user-aaa", profile_name="Profile A"),
            "notebook-1",
        ),
    )

    async def run_once(_ctx, work, *, label):
        assert label == tool.name
        return work()

    monkeypatch.setattr(mcp_tool_host, "_run_with_progress", run_once)
    adapter = mcp_tool_host._plugin_adapter(
        tool,
        host,
        lambda: object(),
        audit_sink=events.append,
    )
    result = await adapter(object(), message="hello", count=1)
    assert result["echo"] == "hello"
    assert result["count"] == 2
    assert events == [
        {
            "kind": "agent_tool_provider",
            "plugin_id": PLUGIN_ID,
            "contribution_id": CONTRIBUTION_ID,
            "tool": "plugin_echo",
            "status": "ok",
        }
    ]
    assert "user-aaa" not in repr(events)
    assert "notebook-1" not in repr(events)

    failing_provider = _Provider((_descriptor(),))

    def fail(*_args):
        raise ValueError("secret provider detail")

    failing_provider.invoke = fail  # type: ignore[method-assign]
    failing_host = _runtime(failing_provider).agent_tools
    failing_tool = failing_host.public_tools[0]
    failure_events: list[dict[str, object]] = []
    failing_adapter = mcp_tool_host._plugin_adapter(
        failing_tool,
        failing_host,
        lambda: object(),
        audit_sink=failure_events.append,
    )
    with pytest.raises(RuntimeError, match="^agent_tool_failed$") as raised:
        await failing_adapter(object(), message="hello", count=1)
    assert "secret provider detail" not in str(raised.value)
    assert failure_events == [
        {
            "kind": "agent_tool_provider",
            "plugin_id": PLUGIN_ID,
            "contribution_id": CONTRIBUTION_ID,
            "tool": "plugin_echo",
            "status": "failed",
        }
    ]

    def broken_audit(_event):
        raise RuntimeError("audit backend unavailable")

    fail_open_adapter = mcp_tool_host._plugin_adapter(
        tool,
        host,
        lambda: object(),
        audit_sink=broken_audit,
    )
    fail_open_result = await fail_open_adapter(
        object(), message="still works", count=1
    )
    assert fail_open_result["echo"] == "still works"

    boundary_events: list[dict[str, object]] = []
    boundary_adapter = mcp_tool_host._plugin_adapter(
        tool,
        host,
        lambda: object(),
        audit_sink=boundary_events.append,
    )
    # FastMCP itself owns missing/wrong schema fields before this handler. The
    # provider-host invalid branch is for schema-valid values that fail its
    # additional wire admission, such as the serialized byte rail.
    with pytest.raises(ValueError, match="configured limit"):
        await boundary_adapter(object(), message="x" * 17_000, count=1)
    assert boundary_events[-1]["status"] == "invalid"

    unavailable_host = _runtime(
        _Provider((_descriptor(),)),
        availability=lambda _context: Availability(
            AvailabilityStatus.UNAVAILABLE, "fixture_disabled"
        ),
    ).agent_tools
    unavailable_adapter = mcp_tool_host._plugin_adapter(
        unavailable_host.public_tools[0],
        unavailable_host,
        lambda: object(),
        audit_sink=boundary_events.append,
    )
    with pytest.raises(PermissionError, match="unavailable"):
        await unavailable_adapter(object(), message="hello", count=1)
    assert boundary_events[-1]["status"] == "unavailable"

    monkeypatch.setattr(
        mcp_tool_host,
        "_selected_notebook",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(PermissionError, match="denied"):
        await boundary_adapter(object(), message="hello", count=1)
    assert boundary_events[-1]["status"] == "denied"


@pytest.mark.anyio
async def test_provider_audit_is_written_to_each_live_token_owner_directory(
    monkeypatch,
    tmp_path,
) -> None:
    from app.api import mcp_tool_host

    provider = _Provider((_descriptor(),))
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    owners = [
        AgentPrincipal(
            profile_id=f"profile-{suffix}",
            profile_name=f"Profile {suffix}",
            owner_id=f"user-{suffix}",
            scopes=["knowledge:read"],
            default_notebook_id="notebook-1",
            notebook_ids=["notebook-1"],
            token_id=f"token-{suffix}",
        )
        for suffix in ("aaa", "bbb")
    ]

    async def run_once(_ctx, work, *, label):
        assert label == tool.name
        return work()

    monkeypatch.setattr(mcp_tool_host, "_run_with_progress", run_once)
    logger = EventLogger(
        SimpleNamespace(
            event_log_enabled=True,
            event_log_dir=str(tmp_path),
            llm_log_max_chars=4_000,
        ),
        channel="events",
        per_user=True,
    )
    adapter = mcp_tool_host._plugin_adapter(
        tool,
        host,
        lambda: object(),
        audit_sink=logger.emit,
    )
    for principal in owners:
        monkeypatch.setattr(
            mcp_tool_host,
            "_selected_notebook",
            lambda *_args, selected=principal: (selected, "notebook-1"),
        )
        await adapter(object(), message="hello", count=1)

    for principal in owners:
        paths = list((tmp_path / principal.owner_id).glob("events-*.jsonl"))
        assert len(paths) == 1
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
        assert payload["kind"] == "agent_tool_provider"
        assert payload["status"] == "ok"
        assert principal.owner_id not in payload
    assert not (tmp_path / "user-local").exists()
    assert not (tmp_path / "_system").exists()


@pytest.mark.anyio
async def test_official_client_enforces_live_read_and_owner_write_policies(
    mcp_env,
    monkeypatch,
) -> None:
    provider = _Provider(
        (
            _descriptor(),
            _descriptor(
                "plugin_write",
                scope="sources:write",
                policy=AgentToolAccessPolicy.OWNER_WRITE,
            ),
        )
    )
    runtime = _runtime(provider)
    from app import main as app_main

    monkeypatch.setattr(app_main, "application_extension_runtime", lambda: runtime)
    app = app_main.create_app()
    notebook_id = mcp_env["notebook"].id
    scopes = ["knowledge:read", "sources:write"]
    owner_token = mcp_env["service"].issue_agent_token(
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        scopes,
        notebook_id,
        [notebook_id],
        None,
    ).token
    member_token = mcp_env["service"].issue_agent_token(
        mcp_env["bob"].id,
        mcp_env["bob_profile"].id,
        scopes,
        notebook_id,
        [notebook_id],
        None,
    ).token

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as anonymous:
            onboarding = await anonymous.get("/api/agent-mcp/onboarding")
            assert onboarding.status_code == 200
            assert "`plugin_echo`" in onboarding.text
            assert "`plugin_write`" in onboarding.text

        async with OfficialMcpClient(
            app, owner_token, manage_lifespan=False
        ) as owner:
            listed = await owner.session.list_tools()
            assert tuple(tool.name for tool in listed.tools) == (
                *CORE_TOOLS,
                "plugin_echo",
                "plugin_write",
            )
            assert (
                await owner.call(
                    "plugin_echo", {"message": "before", "count": 1}
                )
            ).isError
            assert (
                await owner.call(
                    "plugin_echo", {"message": "x" * 17_000, "count": 1}
                )
            ).isError
            assert not any(call[0] == "invoke" for call in provider.calls)
            _payload(
                await owner.call("select_notebook", {"notebook_id": notebook_id})
            )
            echoed = _payload(
                await owner.call(
                    "plugin_echo", {"message": "after", "count": 2}
                )
            )
            assert echoed["echo"] == "after"
            assert not (
                await owner.call(
                    "plugin_write", {"message": "owned", "count": 3}
                )
            ).isError

        async with OfficialMcpClient(
            app, member_token, manage_lifespan=False
        ) as member:
            _payload(
                await member.call("select_notebook", {"notebook_id": notebook_id})
            )
            assert not (
                await member.call(
                    "plugin_echo", {"message": "readable", "count": 4}
                )
            ).isError
            assert (
                await member.call(
                    "plugin_write", {"message": "denied", "count": 5}
                )
            ).isError
