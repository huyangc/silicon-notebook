"""Core-facing contracts for the external-Agent MCP tool catalog.

An Agent token stores *tiers* (``read`` / ``ask`` / ``contribute`` / ``manage``
/ ``delete``): the one vocabulary the token table, the HTTP API and the Agent
access page share. Code keeps asking for *capabilities* (``knowledge:read``,
``sources:write``, ...) at every call site, and the call ledger keeps recording
them; ``AGENT_CAPABILITY_TIER`` is the single place that says which tier grants
which capability.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, Literal

AgentTier = Literal["read", "ask", "contribute", "manage", "delete"]

#: Display order of the tiers (the Agent access page lists them in this order).
AGENT_TIERS: tuple[str, ...] = ("read", "ask", "contribute", "manage", "delete")
AGENT_SCOPES = frozenset(AGENT_TIERS)

AGENT_TIER_LABELS = MappingProxyType(
    {
        "read": "读取",
        "ask": "问答",
        "contribute": "提交",
        "manage": "管理",
        "delete": "删除",
    }
)

AGENT_CAPABILITY_TIER = MappingProxyType(
    {
        "knowledge:read": "read",
        "memory:read": "read",
        "memory:read_candidates": "read",
        "agent_profile:read": "read",
        "ask:execute": "ask",
        "memory:propose": "contribute",
        "knowhow:code": "contribute",
        "agent_observation:write": "contribute",
        "sources:write": "manage",
        "maintenance:execute": "manage",
        "sources:delete": "delete",
    }
)

#: Tiers that only ever act on a notebook the token's owner owns. The owner-only
#: gate itself is ``mcp_tools._shared._writable_notebook``; issuing/editing a
#: token with one of these tiers requires at least one owned notebook in the
#: allowlist (``MemoryService._validate_agent_access``).
AGENT_OWNER_ONLY_TIERS = frozenset({"manage", "delete"})


def capability_tier(capability: str) -> str:
    """The tier that grants ``capability``. An unknown capability is a
    programming error, never a silent deny."""
    try:
        return AGENT_CAPABILITY_TIER[capability]
    except KeyError:
        raise ValueError(f"unknown Agent capability: {capability!r}") from None


def scopes_grant(scopes: Iterable[str], capability: str) -> bool:
    return capability_tier(capability) in {str(scope) for scope in scopes}


def principal_has_capability(principal: Any, capability: str) -> bool:
    """Whether a (freshly refreshed) principal's tiers grant ``capability``."""
    return scopes_grant(getattr(principal, "scopes", ()) or (), capability)


AgentAccessDeniedReason = Literal[
    "inactive", "scope_missing", "notebook_not_allowed", "notebook_unreadable"
]


def scope_missing_message(tier: str) -> str:
    label = AGENT_TIER_LABELS.get(tier, tier)
    return f"此凭证缺少「{label}」权限，请在 Agent 接入页为它勾选后重试"


_DENIED_MESSAGES = {
    "inactive": "此凭证已失效（已撤销、已过期或所属 Agent 已停用），请在 Agent 接入页检查",
    "notebook_not_allowed": "这个笔记本不在此凭证的笔记本白名单里，请在 Agent 接入页把它加入后重试",
    "notebook_unreadable": "凭证主人已无权访问这个笔记本，请确认仍是它的成员",
}


class AgentAccessDenied(PermissionError):
    """A structured refusal from ``MemoryService.require_agent_access``.

    Still a ``PermissionError``, so every caller that maps a deny to 404 (the
    knowhow HTTP routes) keeps doing so without learning why. ``str(exc)`` is
    readable Chinese copy for an Agent; ``reason`` / ``tier`` / ``notebook_id``
    are for code that maps the refusal to an error code."""

    def __init__(
        self,
        reason: AgentAccessDeniedReason,
        *,
        notebook_id: str = "",
        tier: str | None = None,
    ) -> None:
        message = (
            scope_missing_message(tier or "")
            if reason == "scope_missing"
            else _DENIED_MESSAGES[reason]
        )
        super().__init__(message)
        self.reason = reason
        self.tier = tier
        self.notebook_id = notebook_id
