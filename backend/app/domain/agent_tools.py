"""Core-facing contracts for the external-Agent MCP tool catalog."""
from __future__ import annotations

from enum import Enum
from types import MappingProxyType


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
