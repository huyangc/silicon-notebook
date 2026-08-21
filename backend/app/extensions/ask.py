"""Governed post-completion hosts for Ask auditors and observers."""
from __future__ import annotations

from dataclasses import dataclass
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
_STABLE_MODE = re.compile(r"^[a-z][a-z0-9_]*$")
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


class _CoreCompletedAccess:
    """Opaque, at-most-once projection over one core notification port."""

    __slots__ = ("__notify_once", "_called", "_receipt")

    def __init__(self, notify_once: Callable[[], None]) -> None:
        self.__notify_once = notify_once
        self._called = False
        self._receipt: ObserverReceipt | None = None

    @property
    def called(self) -> bool:
        return self._called

    @property
    def receipt(self) -> ObserverReceipt | None:
        return self._receipt

    def notify(self) -> ObserverReceipt:
        if self._called:
            assert self._receipt is not None
            return self._receipt
        self._called = True
        try:
            self.__notify_once()
            receipt = ObserverReceipt(ExtensionResultStatus.AVAILABLE)
        except Exception:
            receipt = ObserverReceipt(
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.FAILED,
                    "ask_completed_notification_failed",
                ),
            )
        self._receipt = receipt
        return receipt


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
        if view is None or not _connection_clear(connection_probe):
            return ()
        accepted: list[AnswerAudit] = []
        sink = event_sink if event_sink is not None else self._event_sink
        availability_context = AnswerAuditAvailabilityContext(
            view.mode_id, view.grounded, view.evidence_level
        )
        for frozen in self._auditors:
            contribution = frozen.registered.contribution
            contribution_id = contribution.declaration.id
            started = _safe_clock(self._clock)
            availability = self._registry.availability(
                contribution_id, availability_context
            )
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(
                    sink,
                    point=ANSWER_AUDITOR_POINT,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=availability.reason_code,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            if not _connection_clear(connection_probe):
                break
            try:
                result = contribution.implementation.audit(
                    AnswerAuditExtensionContext(view)
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
            audit = _valid_auditor_result(result)
            status = "available" if audit is not None else "invalid"
            if audit is not None:
                accepted.append(audit)
            _emit(
                sink,
                point=ANSWER_AUDITOR_POINT,
                contribution_id=contribution_id,
                status=status,
                reason_code=(
                    "" if audit is not None else "invalid_answer_audit_result"
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
        if not _connection_clear(call_context.connection_probe):
            return
        sink = event_sink if event_sink is not None else self._event_sink
        for frozen in self._observers:
            contribution = frozen.registered.contribution
            contribution_id = contribution.declaration.id
            port = _observer_port(call_context, frozen.access_capability)
            notify_once = _core_notify(port)
            access = _CoreCompletedAccess(notify_once) if notify_once else None
            availability_context = AskCompletedAvailabilityContext(
                contribution_id, notification.mode_id, access is not None
            )
            started = _safe_clock(self._clock)
            availability = self._registry.availability(
                contribution_id, availability_context
            )
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(
                    sink,
                    point=ASK_COMPLETED_OBSERVER_POINT,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=availability.reason_code,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            if not _connection_clear(call_context.connection_probe):
                break
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
            if access is not None and access.called:
                receipt = access.receipt
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
                contribution_id=contribution_id,
                status=(
                    receipt.status.value if valid else "invalid"
                ),
                reason_code=(
                    "" if valid else "invalid_observer_receipt"
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
        or not _STABLE_MODE.fullmatch(snapshot.mode_id)
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
        and bool(_STABLE_MODE.fullmatch(value.mode_id))
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
        return context.retrieval_experience
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


def _valid_auditor_result(value: object) -> AnswerAudit | None:
    if (
        type(value) is not AuditorResult
        or type(value.status) is not ExtensionResultStatus
        or not _valid_failure(value.failure)
        or value.status not in {
            ExtensionResultStatus.AVAILABLE,
            ExtensionResultStatus.PARTIAL,
        }
        or type(value.audit) is not AnswerAudit
        or type(value.audit.findings) is not tuple
        or (
            value.status is ExtensionResultStatus.AVAILABLE
            and value.failure is not None
        )
    ):
        return None
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
            return None
    return value.audit


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
        return float(value) if type(value) in {int, float} else None
    except Exception:
        return None


def _elapsed_ms(clock: Callable[[], float], started: float | None) -> int:
    if started is None:
        return 0
    ended = _safe_clock(clock)
    if ended is None:
        return 0
    return max(0, int((ended - started) * 1000))


def _emit(
    sink: Callable[[dict[str, object]], None] | None,
    *,
    point: str,
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
