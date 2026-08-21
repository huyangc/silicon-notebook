"""Point-specific contracts for post-completion Ask extensions.

The durable answer and terminal delivery are core-owned.  Auditors receive a
closed structural snapshot and can only return stable findings; observers see
only the identity dimensions required by their declared capability.  Neither
extension point can rewrite an answer, persist core rows, or acquire a core
database connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
)
from app.extension_sdk.contracts import (
    ActorRef,
    AuditorResult,
    NotebookRef,
    ObserverReceipt,
)


ANSWER_AUDITOR_POINT = "answer.audit"
ASK_COMPLETED_OBSERVER_POINT = "ask.completed_observer"


@dataclass(frozen=True, slots=True)
class AnswerAuditView:
    mode_id: str
    grounded: bool
    evidence_level: str
    citation_count: int
    anchor_count: int
    model_error_count: int
    answer_chars: int
    conclusion_chars: int


@dataclass(frozen=True, slots=True)
class AnswerAuditFinding:
    severity: Literal["info", "warning", "risk"]
    code: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class AnswerAudit:
    findings: tuple[AnswerAuditFinding, ...]


@dataclass(frozen=True, slots=True)
class AnswerAuditAvailabilityContext:
    mode_id: str
    grounded: bool
    evidence_level: str


@dataclass(frozen=True, slots=True)
class AnswerAuditExtensionContext:
    answer: AnswerAuditView


class AnswerAuditor(Protocol):
    def audit(
        self, context: AnswerAuditExtensionContext
    ) -> AuditorResult[AnswerAudit]: ...


class AskCompletedAccess(Protocol):
    def notify(self) -> ObserverReceipt: ...


@dataclass(frozen=True, slots=True)
class AskCompletedAvailabilityContext:
    contribution_id: str
    mode_id: str
    access_available: bool


@dataclass(frozen=True, slots=True)
class AskCompletedExtensionContext:
    """Per-contribution projection; absent identities are structurally hidden."""

    mode_id: str
    actor: ActorRef | None
    notebook: NotebookRef | None
    access: AskCompletedAccess | None


class AskCompletedObserver(Protocol):
    def observe(
        self, context: AskCompletedExtensionContext
    ) -> ObserverReceipt: ...


__all__ = [
    "ANSWER_AUDITOR_POINT",
    "ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY",
    "ASK_COMPLETED_OBSERVER_POINT",
    "ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY",
    "ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY",
    "AnswerAudit",
    "AnswerAuditAvailabilityContext",
    "AnswerAuditExtensionContext",
    "AnswerAuditFinding",
    "AnswerAuditor",
    "AnswerAuditView",
    "AskCompletedAccess",
    "AskCompletedAvailabilityContext",
    "AskCompletedExtensionContext",
    "AskCompletedObserver",
]
