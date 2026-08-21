"""The only bridge from frozen Agent tool catalogs into FastMCP."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from app.api.mcp_tools._shared import (
    _budget_response,
    _run_with_progress,
    _selected_notebook,
    _writable_notebook,
)
from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import register_memory_context_tools
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools
from app.domain.agent_tools import (
    AgentExecutionContext,
    AgentToolAccessPolicy,
    AgentToolProviderHostPort,
    AgentToolValueKind,
    PublishedAgentTool,
    validate_agent_tool_arguments,
)


_CORE_REGISTRARS = (
    register_session_tools,
    register_memory_context_tools,
    register_knowhow_tools,
    register_citation_tools,
    register_source_tools,
    register_maintenance_tools,
    register_profile_tools,
)
_ANNOTATIONS = {
    AgentToolValueKind.STRING: StrictStr,
    AgentToolValueKind.INTEGER: StrictInt,
    AgentToolValueKind.NUMBER: StrictFloat,
    AgentToolValueKind.BOOLEAN: StrictBool,
}


@dataclass(frozen=True, slots=True)
class _CoreTool:
    name: str
    description: str
    handler: Callable[..., object]


class _CoreToolCapture:
    def __init__(self) -> None:
        self.tools: list[_CoreTool] = []
        self._names: set[str] = set()

    def tool(self, *, description: str):
        if type(description) is not str or not description:
            raise RuntimeError("invalid core Agent tool description")

        def register(handler: Callable[..., object]):
            name = getattr(handler, "__name__", "")
            if (
                not callable(handler)
                or type(name) is not str
                or not name
                or name in self._names
            ):
                raise RuntimeError("invalid or duplicate core Agent tool")
            self._names.add(name)
            self.tools.append(_CoreTool(name, description, handler))
            return handler

        return register


def capture_core_agent_tools(
    repository_provider: Callable[[], Any],
) -> tuple[_CoreTool, ...]:
    capture = _CoreToolCapture()
    for registrar in _CORE_REGISTRARS:
        registrar(capture, repository_provider)
    return tuple(capture.tools)


def core_public_tool_names() -> tuple[str, ...]:
    def poison_provider():
        raise AssertionError("core tool discovery must not resolve the repository")

    return tuple(tool.name for tool in capture_core_agent_tools(poison_provider))


def register_agent_tools(
    server: FastMCP,
    repository_provider: Callable[[], Any],
    provider_host: AgentToolProviderHostPort | None,
) -> tuple[str, ...]:
    """Register the core prefix and frozen provider suffix exactly once."""
    core_tools = capture_core_agent_tools(repository_provider)
    names = {tool.name for tool in core_tools}
    for tool in core_tools:
        server.add_tool(tool.handler, name=tool.name, description=tool.description)

    plugin_tools = provider_host.public_tools if provider_host is not None else ()
    if type(plugin_tools) is not tuple or any(
        type(tool) is not PublishedAgentTool for tool in plugin_tools
    ):
        raise RuntimeError("invalid frozen Agent tool catalog")
    for tool in plugin_tools:
        if tool.name in names:
            raise RuntimeError(f"duplicate Agent tool name {tool.name!r}")
        names.add(tool.name)
        server.add_tool(
            _plugin_adapter(tool, provider_host, repository_provider),
            name=tool.name,
            description=tool.description,
        )
    return tuple(tool.name for tool in core_tools) + tuple(
        tool.name for tool in plugin_tools
    )


def _plugin_adapter(
    tool: PublishedAgentTool,
    provider_host: AgentToolProviderHostPort,
    repository_provider: Callable[[], Any],
) -> Callable[..., object]:
    async def invoke(ctx: Context, **arguments: object) -> dict[str, object]:
        validate_agent_tool_arguments(tool, arguments)
        if not provider_host.available(tool):
            raise PermissionError("Agent tool is unavailable")
        repo = repository_provider()
        if tool.access_policy is AgentToolAccessPolicy.READ:
            principal, notebook_id = _selected_notebook(
                ctx, repo, tool.required_scope
            )
        elif tool.access_policy is AgentToolAccessPolicy.OWNER_WRITE:
            principal, notebook_id = _writable_notebook(
                ctx, repo, tool.required_scope
            )
        else:  # pragma: no cover - catalog freeze owns this invariant
            raise PermissionError("Agent tool policy is unavailable")
        context = AgentExecutionContext(
            actor_id=principal.owner_id,
            notebook_id=notebook_id,
            plugin_id=tool.plugin_id,
            tool_name=tool.name,
        )
        frozen_arguments = MappingProxyType(dict(arguments))

        def run() -> dict[str, object]:
            return provider_host.invoke(tool, context, frozen_arguments)

        result = await _run_with_progress(ctx, run, label=tool.name)
        return _budget_response(result)

    invoke.__name__ = tool.name
    invoke.__qualname__ = tool.name
    parameters = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Context,
        )
    ]
    parameters.extend(
        inspect.Parameter(
            parameter.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=_ANNOTATIONS[parameter.kind],
        )
        for parameter in tool.parameters
    )
    invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters,
        return_annotation=dict[str, object],
    )
    return invoke
