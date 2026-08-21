"""Governed post-terminal hosts for Deep Report auditors and observers."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Callable

from app.domain.extensions import (
    REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    CompletedReportNotification,
    ReportAuditSnapshot,
    ReportCompletedObserverCallContext,
)
from app.extension_sdk import (
    REPORT_AUDITOR_POINT,
    REPORT_COMPLETED_OBSERVER_POINT,
    ActorRef,
    AuditorResult,
    AvailabilityStatus,
    ContributionKind,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    NotebookRef,
    ObserverReceipt,
    ReportAudit,
    ReportAuditAvailabilityContext,
    ReportAuditExtensionContext,
    ReportAuditFinding,
    ReportAuditView,
    ReportCompletedAvailabilityContext,
    ReportCompletedExtensionContext,
    ReportRef,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEVERITIES = frozenset({"info", "warning", "risk"})


@dataclass(frozen=True)
class _FrozenAuditor:
    registered: RegisteredContribution


@dataclass(frozen=True)
class _FrozenObserver:
    registered: RegisteredContribution
    has_agent_profile_access: bool


@dataclass
class _CoreAccessState:
    called: bool = False
    receipt: ObserverReceipt | None = None


class _CoreCompletedAccess:
    __slots__ = ("__notify_once",)

    def __init__(self, notify_once: Callable[[], ObserverReceipt]) -> None:
        self.__notify_once = notify_once

    def notify(self) -> ObserverReceipt:
        return self.__notify_once()


class ReportAuditorHost:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError("Report auditor host requires a frozen registry")
        auditors: list[_FrozenAuditor] = []
        for item in registry.contributions(REPORT_AUDITOR_POINT):
            declaration = item.contribution.declaration
            if (
                declaration.kind is not ContributionKind.AUDITOR
                or not callable(getattr(item.contribution.implementation, "audit", None))
            ):
                raise ExtensionRegistryError(
                    f"Report auditor {declaration.id!r} does not implement the contract"
                )
            auditors.append(_FrozenAuditor(item))
        self._registry = registry
        self._auditors = tuple(auditors)
        self._event_sink = event_sink
        self._clock = clock

    @property
    def has_auditors(self) -> bool:
        return bool(self._auditors)

    def audit_application(
        self,
        snapshot: ReportAuditSnapshot,
        *,
        connection_probe: object,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[ReportAudit, ...]:
        if not self._auditors:
            return ()
        view = _report_view(snapshot)
        if (
            view is None
            or not _valid_budget(snapshot.max_findings, snapshot.deadline_monotonic)
            or not _connection_clear(connection_probe)
        ):
            return ()
        sink = event_sink if event_sink is not None else self._event_sink
        accepted: list[ReportAudit] = []
        availability_context = ReportAuditAvailabilityContext(
            view.synthesis_status,
            view.successful_section_count,
            snapshot.deadline_monotonic,
        )
        for frozen in self._auditors:
            registered = frozen.registered
            contribution = registered.contribution
            contribution_id = contribution.declaration.id
            started = _safe_clock(self._clock)
            if not _deadline_open(started, snapshot.deadline_monotonic):
                _emit(sink, REPORT_AUDITOR_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "extension_point_budget_exhausted", 0)
                break
            availability = self._registry.availability(
                contribution_id, availability_context
            )
            if not _connection_clear(connection_probe):
                _emit(sink, REPORT_AUDITOR_POINT, registered.plugin_id,
                      contribution_id, "unavailable", "connection_lease_held",
                      _elapsed_ms(self._clock, started))
                break
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(sink, REPORT_AUDITOR_POINT, registered.plugin_id,
                      contribution_id, "unavailable", availability.reason_code,
                      _elapsed_ms(self._clock, started))
                continue
            if not _deadline_open(_safe_clock(self._clock), snapshot.deadline_monotonic):
                _emit(sink, REPORT_AUDITOR_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "extension_point_budget_exhausted",
                      _elapsed_ms(self._clock, started))
                break
            try:
                result = contribution.implementation.audit(
                    ReportAuditExtensionContext(
                        view, snapshot.max_findings, snapshot.deadline_monotonic
                    )
                )
            except Exception:
                result = AuditorResult(
                    None,
                    ExtensionResultStatus.UNAVAILABLE,
                    ExtensionFailure(
                        ExtensionFailureKind.FAILED, "report_auditor_failed"
                    ),
                )
            valid, audit = _validate_audit(
                result, max_findings=snapshot.max_findings
            )
            if not _connection_clear(connection_probe):
                _emit(sink, REPORT_AUDITOR_POINT, registered.plugin_id,
                      contribution_id, "unavailable", "connection_lease_held",
                      _elapsed_ms(self._clock, started))
                break
            if audit is not None:
                accepted.append(audit)
            _emit(
                sink,
                REPORT_AUDITOR_POINT,
                registered.plugin_id,
                contribution_id,
                result.status.value if valid else "invalid",
                _failure_code(result) if valid else "invalid_report_audit_result",
                _elapsed_ms(self._clock, started),
                len(audit.findings) if audit is not None else 0,
            )
        return tuple(accepted)


class ReportCompletedObserverHost:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Report completed observer host requires a frozen registry"
            )
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        observers: list[_FrozenObserver] = []
        for item in registry.contributions(REPORT_COMPLETED_OBSERVER_POINT):
            declaration = item.contribution.declaration
            if (
                declaration.kind is not ContributionKind.OBSERVER
                or not callable(getattr(item.contribution.implementation, "observe", None))
            ):
                raise ExtensionRegistryError(
                    f"Report observer {declaration.id!r} does not implement the contract"
                )
            requirements = set(manifests[item.plugin_id].requires)
            observers.append(_FrozenObserver(
                item,
                REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY in requirements,
            ))
        self._registry = registry
        self._observers = tuple(observers)
        self._event_sink = event_sink
        self._clock = clock

    def observe_application(
        self,
        call_context: ReportCompletedObserverCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if not self._observers:
            return
        if type(call_context) is not ReportCompletedObserverCallContext:
            return
        notification = call_context.notification
        if (
            not _valid_notification(notification)
            or not _valid_deadline(call_context.deadline_monotonic)
            or not _connection_clear(call_context.connection_probe)
        ):
            return
        sink = event_sink if event_sink is not None else self._event_sink
        for frozen in self._observers:
            registered = frozen.registered
            contribution = registered.contribution
            contribution_id = contribution.declaration.id
            port = call_context.agent_profile if frozen.has_agent_profile_access else None
            started = _safe_clock(self._clock)
            if not _deadline_open(started, call_context.deadline_monotonic):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "extension_point_budget_exhausted", 0)
                break
            availability = self._registry.availability(
                contribution_id,
                ReportCompletedAvailabilityContext(
                    contribution_id,
                    notification.terminal_status,
                    port is not None,
                    call_context.deadline_monotonic,
                ),
            )
            if not _connection_clear(call_context.connection_probe):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable", "connection_lease_held",
                      _elapsed_ms(self._clock, started))
                break
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable", availability.reason_code,
                      _elapsed_ms(self._clock, started))
                continue
            if not _deadline_open(_safe_clock(self._clock), call_context.deadline_monotonic):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "extension_point_budget_exhausted",
                      _elapsed_ms(self._clock, started))
                break
            notify = _core_notify(port)
            if not _connection_clear(call_context.connection_probe):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable", "connection_lease_held",
                      _elapsed_ms(self._clock, started))
                break
            if not _deadline_open(_safe_clock(self._clock), call_context.deadline_monotonic):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "extension_point_budget_exhausted",
                      _elapsed_ms(self._clock, started))
                break
            access, state = _core_access(notify)
            if frozen.has_agent_profile_access and access is None:
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable",
                      "report_completed_access_unavailable",
                      _elapsed_ms(self._clock, started))
                continue
            try:
                receipt = contribution.implementation.observe(
                    ReportCompletedExtensionContext(
                        ReportRef(notification.report_id),
                        ActorRef(notification.actor_id)
                        if frozen.has_agent_profile_access else None,
                        NotebookRef(notification.notebook_id)
                        if frozen.has_agent_profile_access else None,
                        access,
                        call_context.deadline_monotonic,
                    )
                )
            except Exception:
                receipt = ObserverReceipt(
                    ExtensionResultStatus.UNAVAILABLE,
                    ExtensionFailure(
                        ExtensionFailureKind.FAILED,
                        "report_completed_observer_failed",
                    ),
                )
            # A declared core access is mandatory product behavior. A broken
            # plugin cannot suppress it; the host executes the same at-most-once
            # closure when the plugin omitted the call.
            if access is not None and not state.called:
                if not _connection_clear(call_context.connection_probe):
                    _emit(sink, REPORT_COMPLETED_OBSERVER_POINT,
                          registered.plugin_id, contribution_id, "unavailable",
                          "connection_lease_held",
                          _elapsed_ms(self._clock, started))
                    break
                if not _deadline_open(
                    _safe_clock(self._clock), call_context.deadline_monotonic
                ):
                    _emit(sink, REPORT_COMPLETED_OBSERVER_POINT,
                          registered.plugin_id, contribution_id, "unavailable",
                          "extension_point_budget_exhausted",
                          _elapsed_ms(self._clock, started))
                    break
                access.notify()
            if access is not None and state.receipt is not None:
                receipt = state.receipt
            if not _connection_clear(call_context.connection_probe):
                _emit(sink, REPORT_COMPLETED_OBSERVER_POINT, registered.plugin_id,
                      contribution_id, "unavailable", "connection_lease_held",
                      _elapsed_ms(self._clock, started))
                break
            valid = _valid_receipt(receipt)
            _emit(
                sink,
                REPORT_COMPLETED_OBSERVER_POINT,
                registered.plugin_id,
                contribution_id,
                receipt.status.value if valid else "invalid",
                _failure_code(receipt) if valid else "invalid_observer_receipt",
                _elapsed_ms(self._clock, started),
            )


def _report_view(snapshot: object) -> ReportAuditView | None:
    if type(snapshot) is not ReportAuditSnapshot:
        return None
    if (
        type(snapshot.report_id) is not str
        or not snapshot.report_id
        or type(snapshot.synthesis_status) is not str
        or not _STABLE_CODE.fullmatch(snapshot.synthesis_status)
    ):
        return None
    counts = (
        snapshot.section_count,
        snapshot.successful_section_count,
        snapshot.failed_section_count,
        snapshot.reference_count,
        snapshot.gap_count,
        snapshot.claim_ledgers_available,
        snapshot.claim_ledgers_partial,
        snapshot.unsupported_high_risk_assertions,
        snapshot.content_chars,
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    if snapshot.successful_section_count + snapshot.failed_section_count != snapshot.section_count:
        return None
    return ReportAuditView(*counts, snapshot.synthesis_status)


def _valid_notification(value: object) -> bool:
    return (
        type(value) is CompletedReportNotification
        and type(value.report_id) is str and bool(value.report_id)
        and type(value.actor_id) is str and bool(value.actor_id)
        and type(value.notebook_id) is str and bool(value.notebook_id)
        and type(value.terminal_status) is str and value.terminal_status == "done"
    )


def _core_notify(port: object) -> Callable[[], None] | None:
    if port is None:
        return None
    try:
        notify = getattr(port, "notify", None)
    except Exception:
        return None
    return notify if callable(notify) else None


def _core_access(
    notify: Callable[[], None] | None,
) -> tuple[_CoreCompletedAccess | None, _CoreAccessState]:
    state = _CoreAccessState()
    if notify is None:
        return None, state

    def notify_once() -> ObserverReceipt:
        if state.called:
            assert state.receipt is not None
            return state.receipt
        state.called = True
        try:
            notify()
            state.receipt = ObserverReceipt(ExtensionResultStatus.AVAILABLE)
        except Exception:
            state.receipt = ObserverReceipt(
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.FAILED,
                    "report_completed_notification_failed",
                ),
            )
        return state.receipt

    return _CoreCompletedAccess(notify_once), state


def _validate_audit(
    value: object, *, max_findings: int
) -> tuple[bool, ReportAudit | None]:
    if (
        type(value) is not AuditorResult
        or type(value.status) is not ExtensionResultStatus
        or not _valid_failure(value.failure)
    ):
        return False, None
    if value.status is ExtensionResultStatus.UNAVAILABLE:
        return value.audit is None and value.failure is not None, None
    if (
        value.status not in {ExtensionResultStatus.AVAILABLE, ExtensionResultStatus.PARTIAL}
        or type(value.audit) is not ReportAudit
        or type(value.audit.findings) is not tuple
        or len(value.audit.findings) > max_findings
        or (
            value.status is ExtensionResultStatus.AVAILABLE
            and value.failure is not None
        )
    ):
        return False, None
    for finding in value.audit.findings:
        if (
            type(finding) is not ReportAuditFinding
            or type(finding.severity) is not str
            or finding.severity not in _SEVERITIES
            or type(finding.code) is not str
            or not _STABLE_CODE.fullmatch(finding.code)
            or type(finding.count) is not int
            or finding.count < 1
        ):
            return False, None
    return True, value.audit


def _valid_failure(value: object) -> bool:
    return value is None or (
        type(value) is ExtensionFailure
        and type(value.kind) is ExtensionFailureKind
        and type(value.code) is str
        and bool(_STABLE_CODE.fullmatch(value.code))
    )


def _valid_receipt(value: object) -> bool:
    if (
        type(value) is not ObserverReceipt
        or type(value.status) is not ExtensionResultStatus
        or not _valid_failure(value.failure)
    ):
        return False
    return (
        value.failure is None
        if value.status is ExtensionResultStatus.AVAILABLE
        else value.failure is not None
    )


def _failure_code(value: object) -> str:
    failure = getattr(value, "failure", None)
    return failure.code if _valid_failure(failure) and failure is not None else ""


def _connection_clear(probe: object) -> bool:
    try:
        checker = getattr(probe, "is_connection_held", None)
        held = checker() if callable(checker) else None
        return type(held) is bool and not held
    except Exception:
        return False


def _safe_clock(clock: Callable[[], float]) -> float | None:
    try:
        value = clock()
        normalized = float(value) if type(value) in {int, float} else None
        return normalized if normalized is not None and math.isfinite(normalized) else None
    except Exception:
        return None


def _valid_deadline(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value > 0


def _valid_budget(max_findings: object, deadline: object) -> bool:
    return type(max_findings) is int and max_findings > 0 and _valid_deadline(deadline)


def _deadline_open(now: float | None, deadline: float) -> bool:
    return now is None or now <= deadline


def _elapsed_ms(clock: Callable[[], float], started: float | None) -> int:
    if started is None:
        return 0
    ended = _safe_clock(clock)
    if ended is None:
        return 0
    try:
        milliseconds = (ended - started) * 1000
        return max(0, int(milliseconds)) if math.isfinite(milliseconds) else 0
    except (OverflowError, ValueError):
        return 0


def _emit(
    sink: Callable[[dict[str, object]], None] | None,
    point: str,
    plugin_id: str,
    contribution_id: str,
    status: str,
    reason_code: str,
    duration_ms: int,
    count: int = 0,
) -> None:
    if sink is None:
        return
    event: dict[str, object] = {
        "kind": "report_extension",
        "point": point,
        "plugin_id": plugin_id,
        "contribution_id": contribution_id,
        "status": status,
        "duration_ms": duration_ms,
        "count": count,
    }
    if reason_code and _STABLE_CODE.fullmatch(reason_code):
        event["reason_code"] = reason_code
    try:
        sink(event)
    except Exception:
        pass


__all__ = ["ReportAuditorHost", "ReportCompletedObserverHost"]
