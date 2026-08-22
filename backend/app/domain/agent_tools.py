"""Core-facing contracts for the external-Agent MCP tool catalog."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol

AGENT_TOOL_ARGUMENTS_MAX_BYTES = 16_384


class AgentToolValueKind(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class AgentToolAccessPolicy(str, Enum):
    READ = "read"
    OWNER_WRITE = "owner_write"


AGENT_TOOL_SCOPE_POLICIES = MappingProxyType(
    {
        "knowledge:read": AgentToolAccessPolicy.READ,
        "memory:read": AgentToolAccessPolicy.READ,
        "memory:read_candidates": AgentToolAccessPolicy.READ,
        "ask:execute": AgentToolAccessPolicy.READ,
        "agent_profile:read": AgentToolAccessPolicy.READ,
        # Core handlers retain their narrow, explicitly reviewed exceptions. A
        # provider tool cannot inherit those exceptions merely by naming a scope.
        "memory:propose": AgentToolAccessPolicy.OWNER_WRITE,
        "knowhow:code": AgentToolAccessPolicy.OWNER_WRITE,
        "sources:write": AgentToolAccessPolicy.OWNER_WRITE,
        "sources:delete": AgentToolAccessPolicy.OWNER_WRITE,
        "maintenance:execute": AgentToolAccessPolicy.OWNER_WRITE,
        "agent_observation:write": AgentToolAccessPolicy.OWNER_WRITE,
    }
)
AGENT_SCOPES = frozenset(AGENT_TOOL_SCOPE_POLICIES)


@dataclass(frozen=True, slots=True)
class AgentToolParameter:
    name: str
    kind: AgentToolValueKind


@dataclass(frozen=True, slots=True)
class PublishedAgentTool:
    plugin_id: str
    contribution_id: str
    name: str
    description: str
    required_scope: str
    access_policy: AgentToolAccessPolicy
    parameters: tuple[AgentToolParameter, ...]


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    actor_id: str
    notebook_id: str
    plugin_id: str
    tool_name: str


class AgentToolProviderHostPort(Protocol):
    @property
    def public_tools(self) -> tuple[PublishedAgentTool, ...]: ...

    def available(self, tool: PublishedAgentTool) -> bool: ...

    def invoke(
        self,
        tool: PublishedAgentTool,
        context: AgentExecutionContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]: ...


def validate_agent_tool_arguments(
    tool: PublishedAgentTool,
    arguments: Mapping[str, object],
) -> None:
    if type(tool) is not PublishedAgentTool or type(arguments) not in {
        dict,
        MappingProxyType,
    }:
        raise ValueError("invalid Agent tool arguments")
    try:
        snapshot = dict(arguments)
    except Exception as exc:
        raise ValueError("invalid Agent tool arguments") from exc
    if len(snapshot) != len(tool.parameters) or any(
        parameter.name not in snapshot for parameter in tool.parameters
    ):
        raise ValueError("invalid Agent tool arguments")
    expected_types = {
        AgentToolValueKind.STRING: str,
        AgentToolValueKind.INTEGER: int,
        AgentToolValueKind.NUMBER: float,
        AgentToolValueKind.BOOLEAN: bool,
    }
    for parameter in tool.parameters:
        value = snapshot[parameter.name]
        if type(value) is not expected_types[parameter.kind]:
            raise ValueError("invalid Agent tool arguments")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("invalid Agent tool arguments")
    try:
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid Agent tool arguments") from exc
    if len(encoded) > AGENT_TOOL_ARGUMENTS_MAX_BYTES:
        raise ValueError("Agent tool arguments exceed the configured limit")
