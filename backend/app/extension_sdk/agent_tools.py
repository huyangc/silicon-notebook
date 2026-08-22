"""Point-specific SDK contract for trusted in-process Agent tool providers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from app.domain.agent_tools import (
    AGENT_TOOL_ARGUMENTS_MAX_BYTES,
    AgentExecutionContext,
    AgentToolAccessPolicy,
    AgentToolParameter,
    AgentToolValueKind,
)


AGENT_TOOL_PROVIDER_POINT = "agent.tool_provider"
AGENT_TOOL_NAME_MAX_CHARS = 64
AGENT_TOOL_DESCRIPTION_MAX_CHARS = 1_000
AGENT_TOOL_PARAMETERS_MAX = 16
AGENT_TOOL_RESULT_MAX_BYTES = 12_000
AGENT_TOOL_RESULT_MAX_DEPTH = 5


@dataclass(frozen=True, slots=True)
class AgentToolDescriptor:
    name: str
    description: str
    required_scope: str
    access_policy: AgentToolAccessPolicy
    parameters: tuple[AgentToolParameter, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentToolAvailabilityContext:
    plugin_id: str
    contribution_id: str
    tool_name: str


class AgentToolProvider(Protocol):
    def tools(self) -> tuple[AgentToolDescriptor, ...]: ...

    def invoke(
        self,
        tool_name: str,
        context: AgentExecutionContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]: ...


__all__ = [
    "AGENT_TOOL_PROVIDER_POINT",
    "AGENT_TOOL_NAME_MAX_CHARS",
    "AGENT_TOOL_DESCRIPTION_MAX_CHARS",
    "AGENT_TOOL_PARAMETERS_MAX",
    "AGENT_TOOL_RESULT_MAX_BYTES",
    "AGENT_TOOL_RESULT_MAX_DEPTH",
    "AGENT_TOOL_ARGUMENTS_MAX_BYTES",
    "AgentExecutionContext",
    "AgentToolAccessPolicy",
    "AgentToolAvailabilityContext",
    "AgentToolDescriptor",
    "AgentToolParameter",
    "AgentToolProvider",
    "AgentToolValueKind",
]
