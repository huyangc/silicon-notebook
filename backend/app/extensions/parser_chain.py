"""Startup-frozen, request-time parser ProviderChain runner.

PR-04 deliberately leaves this host disconnected from production ingestion.
It establishes the ordering, routing, cancellation and two-phase acceptance
contract that PR-05 will wire into the legacy parser dispatcher in one switch.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable, Generic, TypeVar, cast

from app.domain.cancellation import CoreCancellation
from app.extension_sdk import (
    PARSER_PROVIDER_CHAIN_POINT,
    AvailabilityStatus,
    CancellationToken,
    ContributionKind,
    ExtensionFailure,
    ExtensionFailureKind,
    ParserAdmissionDecision,
    ParserAvailabilityContext,
    ParserExtensionContext,
    ParserHostContext,
    ParserLinkAccess,
    ParserProposal,
    ParserRouteDecision,
    ParserSourceRef,
    ProviderAcceptance,
    ProviderChainAttempt,
    ProviderChainResult,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


T = TypeVar("T")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_KINDS = frozenset({"file", "url"})
_EXECUTION_BOUNDARIES = frozenset({
    "local", "private_service", "public_cloud"
})


class ParserChainCancelled(RuntimeError):
    """Core cancellation when the supplied token has no native raiser."""


class _MalformedCancellationToken(RuntimeError):
    pass


@dataclass(frozen=True)
class _FrozenLink:
    registered: RegisteredContribution
    requires: frozenset[str]


class ParserProviderChainHost(Generic[T]):
    """Run parser links without exposing admission or persistence to plugins."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        cancellation_exceptions: tuple[type[Exception], ...] = (
            CoreCancellation,
        ),
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError("parser chain requires a frozen registry")
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        links: list[_FrozenLink] = []
        for item in registry.contributions(PARSER_PROVIDER_CHAIN_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            if (
                declaration.kind is not ContributionKind.PROVIDER_CHAIN
                or not callable(getattr(implementation, "probe", None))
            ):
                raise ExtensionRegistryError(
                    f"parser link {declaration.id!r} does not implement the parser contract"
                )
            links.append(_FrozenLink(
                registered=item,
                requires=frozenset(manifests[item.plugin_id].requires),
            ))
        self._links = tuple(links)
        self._registry = registry
        self._clock = clock
        self._event_sink = event_sink
        if (
            type(cancellation_exceptions) is not tuple
            or any(
                type(item) is not type or not issubclass(item, Exception)
                for item in cancellation_exceptions
            )
        ):
            raise ExtensionRegistryError(
                "parser cancellation exception types must be exact exception classes"
            )
        self._cancellation_exceptions = cancellation_exceptions

    def run(
        self,
        baseline: T,
        *,
        source: ParserSourceRef,
        route_policy: Callable[[str, ParserSourceRef], ParserRouteDecision],
        context_factory: Callable[[str], ParserHostContext],
        admit: Callable[[str, ParserProposal], ParserAdmissionDecision],
        materialize: Callable[[str, ParserProposal], T],
        cancellation: CancellationToken | None = None,
        warning_sink: Callable[[str], None] | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> ProviderChainResult[T]:
        if not self._links:
            return self._exhausted(baseline, "no_parser_links")
        try:
            return self._run(
                baseline,
                source=source,
                route_policy=route_policy,
                context_factory=context_factory,
                admit=admit,
                materialize=materialize,
                cancellation=cancellation,
                warning_sink=warning_sink,
                event_sink=event_sink,
            )
        except _MalformedCancellationToken:
            return self._exhausted(baseline, "invalid_cancellation_token")

    def _run(
        self,
        baseline: T,
        *,
        source: ParserSourceRef,
        route_policy: Callable[[str, ParserSourceRef], ParserRouteDecision],
        context_factory: Callable[[str], ParserHostContext],
        admit: Callable[[str, ParserProposal], ParserAdmissionDecision],
        materialize: Callable[[str, ParserProposal], T],
        cancellation: CancellationToken | None,
        warning_sink: Callable[[str], None] | None,
        event_sink: Callable[[dict[str, object]], None] | None,
    ) -> ProviderChainResult[T]:
        if not self._valid_source(source):
            return self._exhausted(baseline, "invalid_parser_source")
        if cancellation is None or not self._valid_cancellation(cancellation):
            return self._exhausted(baseline, "invalid_cancellation_token")
        if not all(callable(item) for item in (
            route_policy, context_factory, admit, materialize
        )):
            return self._exhausted(baseline, "invalid_parser_callbacks")

        planned_routes: list[
            tuple[ParserRouteDecision | None, str, str]
        ] = []
        for link in self._links:
            contribution_id = link.registered.contribution.declaration.id
            self._raise_if_cancelled(cancellation)
            try:
                route = route_policy(contribution_id, source)
            except Exception:
                self._raise_if_cancelled(cancellation)
                planned_routes.append(
                    (None, "failed", "route_policy_failed")
                )
                continue
            self._raise_if_cancelled(cancellation)
            if not self._valid_route(route):
                planned_routes.append(
                    (None, "invalid", "invalid_route_decision")
                )
                continue
            planned_routes.append((route, "", ""))

        for link, planned_route in zip(self._links, planned_routes):
            contribution_id = link.registered.contribution.declaration.id
            route, route_outcome, route_failure = planned_route
            if route is None:
                self._emit(
                    contribution_id,
                    route_outcome,
                    route_failure,
                    "",
                    0,
                    event_sink,
                )
                continue
            self._raise_if_cancelled(cancellation)
            if not route.allowed:
                self._emit(
                    contribution_id,
                    "prohibited",
                    self._code(route.reason_code, "route_prohibited"),
                    "",
                    0,
                    event_sink,
                )
                continue

            availability = self._registry.contribution_availability(
                contribution_id,
                ParserAvailabilityContext(
                    plugin_id=link.registered.plugin_id,
                    contribution_id=contribution_id,
                    source=source,
                    cancellation=cancellation,
                ),
            )
            self._raise_if_cancelled(cancellation)
            if availability.status is not AvailabilityStatus.AVAILABLE:
                self._emit(
                    contribution_id,
                    availability.status.value,
                    self._code(
                        availability.reason_code, "parser_link_unavailable"
                    ),
                    "",
                    0,
                    event_sink,
                )
                continue

            try:
                context = context_factory(contribution_id)
            except Exception:
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id, "failed", "parser_context_failed", "", 0,
                    event_sink,
                )
                continue
            self._raise_if_cancelled(cancellation)
            if not self._valid_context(context, contribution_id, source):
                self._emit(
                    contribution_id, "invalid", "invalid_parser_context", "", 0,
                    event_sink,
                )
                continue
            if context.cancellation is not cancellation:
                self._emit(
                    contribution_id,
                    "invalid",
                    "parser_cancellation_identity_mismatch",
                    "",
                    0,
                    event_sink,
                )
                continue
            try:
                connection_held = context.connection.is_connection_held()
            except Exception:
                self._raise_if_cancelled(cancellation)
                connection_held = None
            self._raise_if_cancelled(cancellation)
            if type(connection_held) is not bool or connection_held:
                self._emit(
                    contribution_id,
                    "blocked",
                    (
                        "database_connection_held"
                        if connection_held is True
                        else "invalid_connection_probe"
                    ),
                    "",
                    0,
                    event_sink,
                )
                continue
            if not self._required_capabilities_available(
                link, context, cancellation
            ):
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    "unavailable",
                    "required_capability_unavailable",
                    "",
                    0,
                    event_sink,
                )
                continue
            if not self._valid_access(context.access):
                self._emit(
                    contribution_id,
                    "invalid",
                    "invalid_parser_link_access",
                    "",
                    0,
                    event_sink,
                )
                continue

            plugin_context = ParserExtensionContext(
                source=source,
                cancellation=context.cancellation,
                access=cast(ParserLinkAccess, context.access),
            )
            started = self._started_at()
            try:
                result = link.registered.contribution.implementation.probe(
                    plugin_context
                )
            except TimeoutError:
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    "timeout",
                    "parser_probe_timeout",
                    "",
                    started,
                    event_sink,
                )
                continue
            except Exception:
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    "failed",
                    "parser_probe_failed",
                    "",
                    started,
                    event_sink,
                )
                continue
            self._raise_if_cancelled(cancellation)
            if not self._valid_probe_result(result, contribution_id):
                self._emit(
                    contribution_id,
                    "invalid",
                    "invalid_parser_probe_result",
                    "",
                    started,
                    event_sink,
                )
                continue
            if result.failure is not None:
                if result.failure.kind is ExtensionFailureKind.CANCELLED:
                    self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    result.failure.kind.value,
                    self._code(result.failure.code, "parser_probe_failed"),
                    "",
                    started,
                    event_sink,
                )
                continue
            if result.attempt.acceptance is ProviderAcceptance.REJECT:
                self._emit(
                    contribution_id,
                    "rejected",
                    result.attempt.reason_code,
                    "",
                    started,
                    event_sink,
                )
                continue

            proposal = result.value
            try:
                decision = admit(contribution_id, proposal)
            except Exception:
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    "failed",
                    "parser_admission_failed",
                    "",
                    started,
                    event_sink,
                )
                continue
            self._raise_if_cancelled(cancellation)
            if not self._valid_admission(decision):
                self._emit(
                    contribution_id,
                    "invalid",
                    "invalid_parser_admission",
                    "",
                    started,
                    event_sink,
                )
                continue
            if not decision.accepted:
                self._emit(
                    contribution_id,
                    "rejected",
                    self._code(decision.reason_code, "parser_proposal_rejected"),
                    "",
                    started,
                    event_sink,
                )
                continue

            self._raise_if_cancelled(cancellation)
            try:
                value = materialize(contribution_id, proposal)
            except Exception:
                # PR-05 adapters must make this callback atomic.  The runner
                # never starts another materializer before the current one has
                # returned or raised synchronously.
                self._raise_if_cancelled(cancellation)
                self._emit(
                    contribution_id,
                    "failed",
                    "parser_materialize_failed",
                    "",
                    started,
                    event_sink,
                )
                continue
            self._raise_if_cancelled_after_commit(cancellation)
            warning = route.fallback_warning_code
            self._emit(
                contribution_id,
                "accepted",
                "",
                warning,
                started,
                event_sink,
            )
            if warning and warning_sink is not None:
                try:
                    warning_sink(warning)
                except Exception:
                    pass
            self._raise_if_cancelled_after_commit(cancellation)
            return ProviderChainResult(
                value,
                ProviderChainAttempt(
                    ProviderAcceptance.ACCEPT,
                    reason_code="accepted",
                    warning_code=warning,
                ),
            )
        self._raise_if_cancelled(cancellation)
        return self._exhausted(baseline, "parser_chain_exhausted")

    def _required_capabilities_available(
        self,
        link: _FrozenLink,
        context: ParserHostContext,
        cancellation: CancellationToken | None,
    ) -> bool:
        for capability in sorted(link.requires):
            self._raise_if_cancelled(cancellation)
            availability = self._registry.capability_availability(
                capability, context
            )
            self._raise_if_cancelled(cancellation)
            if availability.status is not AvailabilityStatus.AVAILABLE:
                return False
        return True

    @staticmethod
    def _valid_source(source: object) -> bool:
        return (
            type(source) is ParserSourceRef
            and type(source.kind) is str
            and source.kind in _SOURCE_KINDS
            and type(source.suffix) is str
            and source.suffix.startswith(".")
            and source.suffix == source.suffix.lower()
            and len(source.suffix) > 1
            and source.suffix[1:].isalnum()
        )

    @staticmethod
    def _valid_route(route: object) -> bool:
        return (
            type(route) is ParserRouteDecision
            and type(route.allowed) is bool
            and type(route.execution) is str
            and route.execution in _EXECUTION_BOUNDARIES
            and ParserProviderChainHost._valid_optional_code(route.reason_code)
            and ParserProviderChainHost._valid_optional_code(
                route.fallback_warning_code
            )
            and (route.allowed or not route.fallback_warning_code)
        )

    @staticmethod
    def _valid_context(
        context: object, contribution_id: str, source: ParserSourceRef
    ) -> bool:
        try:
            return (
                type(context) is ParserHostContext
                and type(context.contribution_id) is str
                and context.contribution_id == contribution_id
                and context.source is source
                and ParserProviderChainHost._valid_cancellation(
                    context.cancellation
                )
                and callable(
                    getattr(context.connection, "is_connection_held", None)
                )
            )
        except Exception:
            return False

    @staticmethod
    def _valid_access(access: object) -> bool:
        try:
            return callable(getattr(access, "probe", None))
        except Exception:
            return False

    @staticmethod
    def _valid_probe_result(result: object, contribution_id: str) -> bool:
        try:
            if (
                type(result) is not ProviderChainResult
                or type(result.attempt) is not ProviderChainAttempt
                or type(result.attempt.acceptance) is not ProviderAcceptance
                or not ParserProviderChainHost._valid_optional_code(
                    result.attempt.reason_code
                )
                or type(result.attempt.warning_code) is not str
                or result.attempt.warning_code != ""
            ):
                return False
            failure = result.failure
            if failure is not None:
                return (
                    type(failure) is ExtensionFailure
                    and type(failure.kind) is ExtensionFailureKind
                    and ParserProviderChainHost._valid_required_code(failure.code)
                    and result.value is None
                    and result.attempt.acceptance is ProviderAcceptance.REJECT
                )
            if result.attempt.acceptance is ProviderAcceptance.REJECT:
                return (
                    result.value is None
                    and ParserProviderChainHost._valid_required_code(
                        result.attempt.reason_code
                    )
                )
            return (
                type(result.value) is ParserProposal
                and type(result.value.contribution_id) is str
                and result.value.contribution_id == contribution_id
                and result.attempt.reason_code in {"", "accepted"}
            )
        except Exception:
            return False

    @staticmethod
    def _valid_admission(decision: object) -> bool:
        return (
            type(decision) is ParserAdmissionDecision
            and type(decision.accepted) is bool
            and ParserProviderChainHost._valid_optional_code(decision.reason_code)
            and (
                decision.accepted
                or ParserProviderChainHost._valid_required_code(
                    decision.reason_code
                )
            )
        )

    @staticmethod
    def _valid_cancellation(cancellation: object) -> bool:
        try:
            return callable(getattr(cancellation, "is_set", None)) and (
                getattr(cancellation, "raise_if_cancelled", None) is None
                or callable(getattr(cancellation, "raise_if_cancelled", None))
            )
        except Exception:
            return False

    def _raise_if_cancelled(
        self, cancellation: CancellationToken | None
    ) -> None:
        if cancellation is None:
            return
        try:
            state = cancellation.is_set()
        except Exception as exc:
            raise _MalformedCancellationToken() from exc
        if type(state) is not bool:
            raise _MalformedCancellationToken()
        if not state:
            return
        try:
            native = getattr(cancellation, "raise_if_cancelled", None)
        except Exception as exc:
            raise _MalformedCancellationToken() from exc
        if native is not None:
            try:
                native()
            except Exception as exc:
                try:
                    confirmed = cancellation.is_set()
                except Exception as confirm_exc:
                    raise _MalformedCancellationToken() from confirm_exc
                if (
                    type(confirmed) is bool
                    and confirmed
                    and isinstance(exc, self._cancellation_exceptions)
                ):
                    raise
                raise _MalformedCancellationToken() from exc
        raise ParserChainCancelled()

    def _raise_if_cancelled_after_commit(
        self, cancellation: CancellationToken
    ) -> None:
        """Never describe a completed commit as rejected for a hostile token."""

        try:
            self._raise_if_cancelled(cancellation)
        except _MalformedCancellationToken:
            return

    @staticmethod
    def _valid_optional_code(value: object) -> bool:
        return type(value) is str and (
            not value or _STABLE_CODE.fullmatch(value) is not None
        )

    @staticmethod
    def _valid_required_code(value: object) -> bool:
        return (
            type(value) is str
            and _STABLE_CODE.fullmatch(value) is not None
        )

    @staticmethod
    def _code(value: object, fallback: str) -> str:
        return (
            value
            if ParserProviderChainHost._valid_required_code(value)
            else fallback
        )

    @staticmethod
    def _exhausted(baseline: T, code: str) -> ProviderChainResult[T]:
        return ProviderChainResult(
            baseline,
            ProviderChainAttempt(
                ProviderAcceptance.REJECT,
                reason_code=code,
            ),
            ExtensionFailure(ExtensionFailureKind.REJECTED, code),
        )

    def _emit(
        self,
        contribution_id: str,
        outcome: str,
        failure_code: str,
        warning_code: str,
        started: float,
        call_sink: Callable[[dict[str, object]], None] | None,
    ) -> None:
        elapsed = 0
        if started:
            try:
                elapsed = max(0, int((self._clock() - started) * 1000))
            except Exception:
                elapsed = 0
        event = {
            "kind": "parser_provider_chain_attempt",
            "contribution_id": contribution_id,
            "outcome": self._code(outcome, "failed"),
            "failure_code": (
                self._code(failure_code, "parser_chain_failed")
                if failure_code else ""
            ),
            "warning_code": (
                self._code(warning_code, "parser_fallback")
                if warning_code else ""
            ),
            "elapsed_ms": elapsed,
        }
        for sink in (call_sink, self._event_sink):
            if sink is None:
                continue
            try:
                sink(dict(event))
            except Exception:
                pass

    def _started_at(self) -> float:
        try:
            value = self._clock()
        except Exception:
            return 0
        return value if type(value) in {float, int} else 0


__all__ = ["ParserChainCancelled", "ParserProviderChainHost"]
