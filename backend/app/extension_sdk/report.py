"""Point-specific contracts for post-terminal Deep Report extensions.

The durable report is already committed before the point runs. Observers
receive only the identities required by their declared capability and cannot
rewrite report artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.extensions import REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
from app.extension_sdk.contracts import (
    ActorRef,
    NotebookRef,
    ObserverReceipt,
)


REPORT_COMPLETED_OBSERVER_POINT = "report.completed_observer"


class ReportCompletedAccess(Protocol):
    def notify(self) -> ObserverReceipt: ...


@dataclass(frozen=True, slots=True)
class ReportRef:
    """Opaque identity for the already-durable completed report."""

    id: str
    terminal_status: Literal["done"] = "done"


@dataclass(frozen=True, slots=True)
class ReportCompletedAvailabilityContext:
    contribution_id: str
    terminal_status: str
    access_available: bool
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ReportCompletedExtensionContext:
    report: ReportRef
    actor: ActorRef | None
    notebook: NotebookRef | None
    access: ReportCompletedAccess | None
    deadline_monotonic: float


class ReportCompletedObserver(Protocol):
    def observe(
        self, context: ReportCompletedExtensionContext
    ) -> ObserverReceipt: ...


__all__ = [
    "REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY",
    "REPORT_COMPLETED_OBSERVER_POINT",
    "ReportCompletedAccess",
    "ReportCompletedAvailabilityContext",
    "ReportCompletedExtensionContext",
    "ReportCompletedObserver",
    "ReportRef",
]
