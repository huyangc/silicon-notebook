"""Stable identity and human-readable audit actor semantics."""
from __future__ import annotations

from dataclasses import dataclass

from app.models.identity import UserProfile


@dataclass(frozen=True)
class AuditPrincipal:
    """Keep authorization identity separate from immutable audit text."""

    identity_id: str
    audit_label: str


def session_audit_principal(user: UserProfile) -> AuditPrincipal:
    """Return the one canonical session-user audit label fallback chain."""

    username = str(user.username or "").strip()
    display_name = str(user.display_name or "").strip()
    return AuditPrincipal(
        identity_id=user.id,
        audit_label=username or display_name or user.id,
    )


__all__ = ["AuditPrincipal", "session_audit_principal"]
