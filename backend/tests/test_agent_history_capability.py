"""The ``history:read`` capability: which tier grants it, and why that tier is pinned.

``history:read`` lets an Agent credential read its owner's own Q&A conversations
and deep reports (MCP list/get_conversation, list/get_report). It sits in the
``read`` tier, the same tier as ``memory:read`` -- and that is an invariant, not
an accident: a stored single-notebook answer may come from a run that opened the
private-memory channel, and a report may quote its author's memory. ``get_ask``
refuses such content unless the credential holds ``memory:read`` right now. The
history tools do NOT carry that replay gate, because any credential that passes
their tool gate (``history:read``) already holds ``memory:read``.
"""
from __future__ import annotations

from app.domain.agent_tools import capability_tier, scopes_grant


def test_history_reads_share_the_memory_tier():
    assert capability_tier("history:read") == capability_tier("memory:read") == "read", (
        "history:read and memory:read must stay in the same tier ('read'): "
        "get_conversation / get_report read stored answers and reports without "
        "get_ask's private-memory replay gate, which is only safe while holding "
        "history:read implies holding memory:read. If the tiers are ever split, "
        "give get_conversation / get_report the memory replay gate that "
        "get_ask has (ask.py::_notebook_delivery_gate) before shipping the split."
    )


def test_history_read_is_granted_by_read_alone():
    assert scopes_grant(["read"], "history:read") is True
    for tier in ("ask", "contribute", "manage", "delete"):
        assert scopes_grant([tier], "history:read") is False, tier
    assert scopes_grant(["ask", "contribute", "manage", "delete"], "history:read") is False
