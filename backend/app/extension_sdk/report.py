"""Point-specific contracts for post-terminal Deep Report extensions.

The durable report is already committed before either point runs. Auditors can
only return bounded structural findings; observers receive only the identities
required by their declared capability and cannot rewrite report artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.extensions import REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
from app.extension_sdk.contracts import (
    ActorRef,
    AuditorResult,
    NotebookRef,
    ObserverReceipt,
)


REPORT_AUDITOR_POINT = "report.audit"
REPORT_COMPLETED_OBSERVER_POINT = "report.completed_observer"


@dataclass(frozen=True, slots=True)
class ReportAuditView:
    section_count: int
    successful_section_count: int
    failed_section_count: int
    reference_count: int
    gap_count: int
    claim_ledgers_available: int
    claim_ledgers_partial: int
    unsupported_high_risk_assertions: int
    content_chars: int
    synthesis_status: str


@dataclass(frozen=True, slots=True)
class ReportAuditFinding:
    severity: Literal["info", "warning", "risk"]
    code: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class ReportAudit:
    findings: tuple[ReportAuditFinding, ...]


@dataclass(frozen=True, slots=True)
class ReportAuditAvailabilityContext:
    synthesis_status: str
    successful_section_count: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ReportAuditExtensionContext:
    report: ReportAuditView
    max_findings: int
    deadline_monotonic: float


class ReportAuditor(Protocol):
    def audit(
        self, context: ReportAuditExtensionContext
    ) -> AuditorResult[ReportAudit]: ...


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
    "REPORT_AUDITOR_POINT",
    "REPORT_COMPLETED_OBSERVER_POINT",
    "ReportAudit",
    "ReportAuditAvailabilityContext",
    "ReportAuditExtensionContext",
    "ReportAuditFinding",
    "ReportAuditor",
    "ReportAuditView",
    "ReportCompletedAccess",
    "ReportCompletedAvailabilityContext",
    "ReportCompletedExtensionContext",
    "ReportCompletedObserver",
    "ReportRef",
]
