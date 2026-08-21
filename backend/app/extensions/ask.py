"""Governed post-completion hosts for Ask auditors and observers."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Callable

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    AgentProfileAskCompletedPort,
    AnswerAuditSnapshot,
    AskCompletedObserverCallContext,
    CompletedAskNotification,
    RetrievalExperienceAskCompletedPort,
    SearchProfileAskCompletedPort,
)
from app.extension_sdk import (
    ANSWER_AUDITOR_POINT,
    ASK_COMPLETED_OBSERVER_POINT,
    ActorRef,
    AuditorResult,
    AvailabilityStatus,
    ContributionKind,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    NotebookRef,
    ObserverReceipt,
    AnswerAudit,
    AnswerAuditAvailabilityContext,
    AnswerAuditExtensionContext,
    AnswerAuditFinding,
    AnswerAuditView,
    AskCompletedAvailabilityContext,
    AskCompletedExtensionContext,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEVERITIES = frozenset({"info", "warning", "risk"})
_OBSERVER_ACCESS_CAPABILITIES = frozenset({
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
})


@dataclass(frozen=True)
class _FrozenAuditor:
    registered: RegisteredContribution


@dataclass(frozen=True)
class _FrozenObserver:
    registered: RegisteredContribution
    access_capability: str | None


@dataclass
class _CoreAccessState:
    called: bool = False
    receipt: ObserverReceipt | None = None


class _CoreCompletedAccess:
    """Opaque, at-most-once projection over one core notification port."""

    __slots__ = ("__notify_once",)

    def __init__(self, notify_once: Callable[[], ObserverReceipt]) -> None:
        self.__notify_once = notify_once

    def notify(self) -> ObserverReceipt:
        return self.__notify_once()


class AnswerAuditorHost:
    """Run optional auditors without exposing or replacing ``AskResponse``."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "answer auditor host requires a frozen registry"
            )
        auditors: list[_FrozenAuditor] = []
        for item in registry.contributions(ANSWER_AUDITOR_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            if (
                declaration.kind is not ContributionKind.AUDITOR
                or not callable(getattr(implementation, "audit", None))
            ):
                raise ExtensionRegistryError(
                    f"answer auditor {declaration.id!r} does not implement "
                    "the answer auditor contract"
                )
            auditors.append(_FrozenAuditor(item))
        self._registry = registry
        self._auditors = tuple(auditors)
        self._event_sink = event_sink
        self._clock = clock

    @property
    def has_auditors(self) -> bool:
        """Startup topology fact used by the outer root to keep empty paths inert."""
        return bool(self._auditors)

    def audit_application(
        self,
        snapshot: AnswerAuditSnapshot,
        *,
        connection_probe: object,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[AnswerAudit, ...]:
        # Empty topology is a strict no-op: no validation, probe, clock or event.
        if not self._auditors:
            return ()
        view = _answer_view(snapshot)
        if (
            view is None
            or not _valid_budget(snapshot.max_findings, snapshot.deadline_monotonic)
            or not _connection_clear(connection_probe)
        ):
            return ()
        accepted: list[AnswerAudit] = []
        sink = event_sink if event_sink is not None else self._event_sink
        availability_context = AnswerAuditAvailabilityContext(
            view.mode_id,
            view.grounded,
            view.evidence_level,
            snapshot.deadline_monotonic,
        )
        for frozen in self._auditors:
            contribution = frozen.registered.contribution
            contribution_id = contribution.declaration.id
            plugin_id = frozen.registered.plugin_id
            started = _safe_clock(self._clock)
            if not _deadline_open(started, snapshot.deadline_monotonic):
                _emit(
                    sink,
                    point=ANSWER_AUDITOR_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="extension_point_budget_exhausted",
                    duration_ms=0,
                )
                break
            availability = self._registry.availability(
                contribution_id, availability_context
            )
            if not _connection_clear(connection_probe):
                _emit(
                    sink,
                    point=ANSWER_AUDITOR_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="connection_lease_held",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                break
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(
                    sink,
                    point=ANSWER_AUDITOR_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=availability.reason_code,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            if not _deadline_open(_safe_clock(self._clock), snapshot.deadline_monotonic):
                break
            try:
                result = contribution.implementation.audit(
                    AnswerAuditExtensionContext(
                        view,
                        snapshot.max_findings,
                        snapshot.deadline_monotonic,
                    )
                )
            except Exception:
                result = AuditorResult(
                    None,
                    ExtensionResultStatus.UNAVAILABLE,
                    ExtensionFailure(
                        ExtensionFailureKind.FAILED,
                        "answer_auditor_failed",
                    ),
                )
            valid, audit = _validate_auditor_result(
                result, max_findings=snapshot.max_findings
            )
            status = result.status.value if valid else "invalid"
            if audit is not None:
                accepted.append(audit)
            _emit(
                sink,
                point=ANSWER_AUDITOR_POINT,
                plugin_id=plugin_id,
                contribution_id=contribution_id,
                status=status,
                reason_code=(
                    _failure_code(result)
                    if valid
                    else "invalid_answer_audit_result"
                ),
                duration_ms=_elapsed_ms(self._clock, started),
                count=len(audit.findings) if audit is not None else 0,
            )
            if not _connection_clear(connection_probe):
                break
        return tuple(accepted)


class AskCompletedObserverHost:
    """Run completion observers after durable final delivery, one by one."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Ask completed observer host requires a frozen registry"
            )
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        observers: list[_FrozenObserver] = []
        for item in registry.contributions(ASK_COMPLETED_OBSERVER_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            if (
                declaration.kind is not ContributionKind.OBSERVER
                or not callable(getattr(implementation, "observe", None))
            ):
                raise ExtensionRegistryError(
                    f"Ask completed observer {declaration.id!r} does not "
                    "implement the observer contract"
                )
            capabilities = (
                set(manifests[item.plugin_id].requires)
                & _OBSERVER_ACCESS_CAPABILITIES
            )
            if len(capabilities) > 1:
                raise ExtensionRegistryError(
                    f"Ask completed observer {declaration.id!r} requests "
                    "multiple core notification capabilities"
                )
            observers.append(_FrozenObserver(
                item, next(iter(capabilities), None)
            ))
        self._registry = registry
        self._observers = tuple(observers)
        self._event_sink = event_sink
        self._clock = clock

    def observe_application(
        self,
        call_context: AskCompletedObserverCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        # Empty topology is a strict no-op, matching the historical no-plugin path.
        if not self._observers:
            return
        if type(call_context) is not AskCompletedObserverCallContext:
            return
        notification = call_context.notification
        if not _valid_notification(notification):
            return
        if not _valid_deadline(call_context.deadline_monotonic):
            return
        if not _connection_clear(call_context.connection_probe):
            return
        sink = event_sink if event_sink is not None else self._event_sink
        for frozen in self._observers:
            contribution = frozen.registered.contribution
            contribution_id = contribution.declaration.id
            plugin_id = frozen.registered.plugin_id
            port = _observer_port(call_context, frozen.access_capability)
            availability_context = AskCompletedAvailabilityContext(
                contribution_id,
                notification.mode_id,
                port is not None,
                call_context.deadline_monotonic,
            )
            started = _safe_clock(self._clock)
            if not _deadline_open(started, call_context.deadline_monotonic):
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="extension_point_budget_exhausted",
                    duration_ms=0,
                )
                break
            availability = self._registry.availability(
                contribution_id, availability_context
            )
            if not _connection_clear(call_context.connection_probe):
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="connection_lease_held",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                break
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=availability.reason_code,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            if not _deadline_open(
                _safe_clock(self._clock), call_context.deadline_monotonic
            ):
                break
            notify = _core_notify(port)
            # Resolving a core port is still a boundary operation: a hostile or
            # accidentally lazy property must not smuggle a newly-held lease or
            # consume the remaining point budget before plugin execution.
            if not _connection_clear(call_context.connection_probe):
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="connection_lease_held",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                break
            if not _deadline_open(
                _safe_clock(self._clock), call_context.deadline_monotonic
            ):
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="extension_point_budget_exhausted",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                break
            access, access_state = _core_access(notify)
            if frozen.access_capability is not None and access is None:
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="ask_completed_access_unavailable",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            actor, notebook = _observer_identities(
                notification, frozen.access_capability
            )
            try:
                receipt = contribution.implementation.observe(
                    AskCompletedExtensionContext(
                        notification.mode_id,
                        actor,
                        notebook,
                        access,
                        call_context.deadline_monotonic,
                    )
                )
            except Exception:
                receipt = ObserverReceipt(
                    ExtensionResultStatus.UNAVAILABLE,
                    ExtensionFailure(
                        ExtensionFailureKind.FAILED,
                        "ask_completed_observer_failed",
                    ),
                )
            if access is not None and access_state.called:
                receipt = access_state.receipt
            elif frozen.access_capability is not None:
                receipt = ObserverReceipt(
                    ExtensionResultStatus.UNAVAILABLE,
                    ExtensionFailure(
                        ExtensionFailureKind.INVALID_RESULT,
                        "ask_completed_access_not_called",
                    ),
                )
            valid = _valid_observer_receipt(receipt)
            _emit(
                sink,
                point=ASK_COMPLETED_OBSERVER_POINT,
                plugin_id=plugin_id,
                contribution_id=contribution_id,
                status=(
                    receipt.status.value if valid else "invalid"
                ),
                reason_code=(
                    _failure_code(receipt)
                    if valid
                    else "invalid_observer_receipt"
                ),
                duration_ms=_elapsed_ms(self._clock, started),
            )
            if not _connection_clear(call_context.connection_probe):
                break


def _answer_view(snapshot: object) -> AnswerAuditView | None:
    if type(snapshot) is not AnswerAuditSnapshot:
        return None
    if (
        type(snapshot.mode_id) is not str
        or not _STABLE_CODE.fullmatch(snapshot.mode_id)
        or type(snapshot.grounded) is not bool
        or type(snapshot.evidence_level) is not str
        or not _STABLE_CODE.fullmatch(snapshot.evidence_level)
    ):
        return None
    counts = (
        snapshot.citation_count,
        snapshot.anchor_count,
        snapshot.model_error_count,
        snapshot.answer_chars,
        snapshot.conclusion_chars,
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    return AnswerAuditView(
        snapshot.mode_id,
        snapshot.grounded,
        snapshot.evidence_level,
        *counts,
    )


def _valid_notification(value: object) -> bool:
    return (
        type(value) is CompletedAskNotification
        and type(value.actor_id) is str
        and bool(value.actor_id)
        and type(value.notebook_id) is str
        and bool(value.notebook_id)
        and type(value.mode_id) is str
        and bool(_STABLE_CODE.fullmatch(value.mode_id))
    )


def _observer_port(
    context: AskCompletedObserverCallContext,
    capability: str | None,
) -> (
    AgentProfileAskCompletedPort
    | RetrievalExperienceAskCompletedPort
    | SearchProfileAskCompletedPort
    | None
):
    if capability == ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY:
        return context.agent_profile
    if capability == ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY:
        return (
            context.retrieval_experience
            if context.notification.mode_id == "reasoning"
            else None
        )
    if capability == ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY:
        return context.search_profile
    return None


def _observer_identities(
    notification: CompletedAskNotification,
    capability: str | None,
) -> tuple[ActorRef | None, NotebookRef | None]:
    if capability == ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY:
        return ActorRef(notification.actor_id), NotebookRef(notification.notebook_id)
    if capability == ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY:
        return ActorRef(notification.actor_id), None
    return None, None


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
            receipt = ObserverReceipt(ExtensionResultStatus.AVAILABLE)
        except Exception:
            receipt = ObserverReceipt(
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.FAILED,
                    "ask_completed_notification_failed",
                ),
            )
        state.receipt = receipt
        return receipt

    return _CoreCompletedAccess(notify_once), state


def _valid_failure(value: object) -> bool:
    return (
        value is None
        or (
            type(value) is ExtensionFailure
            and type(value.kind) is ExtensionFailureKind
            and type(value.code) is str
            and bool(_STABLE_CODE.fullmatch(value.code))
        )
    )


def _validate_auditor_result(
    value: object,
    *,
    max_findings: int,
) -> tuple[bool, AnswerAudit | None]:
    if (
        type(value) is not AuditorResult
        or type(value.status) is not ExtensionResultStatus
        or not _valid_failure(value.failure)
    ):
        return False, None
    if value.status is ExtensionResultStatus.UNAVAILABLE:
        return value.audit is None and value.failure is not None, None
    if (
        value.status not in {
            ExtensionResultStatus.AVAILABLE,
            ExtensionResultStatus.PARTIAL,
        }
        or type(value.audit) is not AnswerAudit
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
            type(finding) is not AnswerAuditFinding
            or type(finding.severity) is not str
            or finding.severity not in _SEVERITIES
            or type(finding.code) is not str
            or not _STABLE_CODE.fullmatch(finding.code)
            or type(finding.count) is not int
            or finding.count < 1
        ):
            return False, None
    return True, value.audit


def _valid_observer_receipt(value: object) -> bool:
    if not (
        type(value) is ObserverReceipt
        and type(value.status) is ExtensionResultStatus
        and _valid_failure(value.failure)
    ):
        return False
    if value.status is ExtensionResultStatus.AVAILABLE:
        return value.failure is None
    if value.status is ExtensionResultStatus.UNAVAILABLE:
        return value.failure is not None
    return True


def _failure_code(value: object) -> str:
    failure = getattr(value, "failure", None)
    return failure.code if _valid_failure(failure) and failure is not None else ""


def _connection_clear(probe: object) -> bool:
    try:
        checker = getattr(probe, "is_connection_held", None)
        if not callable(checker):
            return False
        held = checker()
        return type(held) is bool and not held
    except Exception:
        return False


def _safe_clock(clock: Callable[[], float]) -> float | None:
    try:
        value = clock()
        normalized = float(value) if type(value) in {int, float} else None
        return (
            normalized
            if normalized is not None and math.isfinite(normalized)
            else None
        )
    except Exception:
        return None


def _valid_deadline(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value > 0


def _valid_budget(max_findings: object, deadline: object) -> bool:
    return (
        type(max_findings) is int
        and max_findings > 0
        and _valid_deadline(deadline)
    )


def _deadline_open(now: float | None, deadline: float) -> bool:
    # Timing is observability/admission metadata, never a reason to let a
    # broken injected clock suppress legacy completion behavior.
    return now is None or now <= deadline


def _elapsed_ms(clock: Callable[[], float], started: float | None) -> int:
    if started is None:
        return 0
    ended = _safe_clock(clock)
    if ended is None:
        return 0
    try:
        delta = ended - started
        milliseconds = delta * 1000
        if not math.isfinite(delta) or not math.isfinite(milliseconds):
            return 0
        return max(0, int(milliseconds))
    except (OverflowError, ValueError):
        return 0


def _emit(
    sink: Callable[[dict[str, object]], None] | None,
    *,
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
        "kind": "ask_extension",
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


__all__ = ["AnswerAuditorHost", "AskCompletedObserverHost"]
