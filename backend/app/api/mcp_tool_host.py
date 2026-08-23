"""The only bridge from the frozen core Agent tool catalog into FastMCP."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import register_memory_context_tools
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools


_CORE_REGISTRARS = (
    register_session_tools,
    register_memory_context_tools,
    register_knowhow_tools,
    register_citation_tools,
    register_source_tools,
    register_maintenance_tools,
    register_profile_tools,
)


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
) -> tuple[str, ...]:
    """Register the frozen core Agent tool prefix exactly once."""
    core_tools = capture_core_agent_tools(repository_provider)
    names = tuple(tool.name for tool in core_tools)
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate Agent tool name")
    for tool in core_tools:
        server.add_tool(tool.handler, name=tool.name, description=tool.description)
    return names
