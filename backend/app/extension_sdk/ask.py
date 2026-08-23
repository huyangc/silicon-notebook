"""Point-specific contracts for post-completion Ask extensions.

The durable answer and terminal delivery are core-owned.  Observers see only
the identity dimensions required by their declared capability and cannot
rewrite an answer, persist core rows, or acquire a core database connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
)
from app.extension_sdk.contracts import (
    ActorRef,
    NotebookRef,
    ObserverReceipt,
)


ASK_COMPLETED_OBSERVER_POINT = "ask.completed_observer"


class AskCompletedAccess(Protocol):
    def notify(self) -> ObserverReceipt: ...


@dataclass(frozen=True, slots=True)
class AskCompletedAvailabilityContext:
    contribution_id: str
    mode_id: str
    access_available: bool
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class AskCompletedExtensionContext:
    """Per-contribution projection; absent identities are structurally hidden."""

    mode_id: str
    actor: ActorRef | None
    notebook: NotebookRef | None
    access: AskCompletedAccess | None
    deadline_monotonic: float


class AskCompletedObserver(Protocol):
    def observe(
        self, context: AskCompletedExtensionContext
    ) -> ObserverReceipt: ...


__all__ = [
    "ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY",
    "ASK_COMPLETED_OBSERVER_POINT",
    "ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY",
    "ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY",
    "AskCompletedAccess",
    "AskCompletedAvailabilityContext",
    "AskCompletedExtensionContext",
    "AskCompletedObserver",
]
