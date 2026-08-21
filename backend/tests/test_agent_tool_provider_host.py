from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

import httpx
import pytest

from app.api.mcp_server import PUBLIC_TOOLS, create_memory_mcp, mcp_public_tools
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
from app.extensions import build_extension_runtime
from app.extensions.registry import ExtensionRegistryError
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
    assert tuple(tool.name for tool in tools) == (*PUBLIC_TOOLS, "plugin_echo")
    assert mcp_public_tools(server) == (*PUBLIC_TOOLS, "plugin_echo")
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


def test_provider_failure_preserves_its_exception_identity_and_message() -> None:
    provider = _Provider((_descriptor(),))
    failure = ValueError("safe provider failure")

    def fail(*_args):
        raise failure

    provider.invoke = fail  # type: ignore[method-assign]
    host = _runtime(provider).agent_tools
    tool = host.public_tools[0]
    with pytest.raises(ValueError, match="safe provider failure") as raised:
        host.invoke(
            tool,
            AgentExecutionContext("actor", "notebook", PLUGIN_ID, tool.name),
            MappingProxyType({"message": "hello", "count": 1}),
        )
    assert raised.value is failure


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
                *PUBLIC_TOOLS,
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
