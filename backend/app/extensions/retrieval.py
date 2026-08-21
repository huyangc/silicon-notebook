"""Baseline-preserving runner for ``retrieval.contributor`` extensions."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import re
import time
from typing import Any, TypeVar

from app.extension_sdk import (
    AvailabilityStatus,
    EvidenceCandidate,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    RETRIEVAL_CONTRIBUTOR_POINT,
    RetrievalContributionEvent,
    RetrievalExtensionContext,
    RetrievalInvocation,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


T = TypeVar("T")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_PROVENANCE_KINDS = frozenset({
    "chunk",
    "element",
    "knowledge_object",
    "relation",
    "ppr",
})
_INVOCATIONS = frozenset({"selected_evidence", "chunk_candidates"})


class RetrievalHostCancelled(RuntimeError):
    """Core request cancellation; callers must propagate, never fail open."""


class RetrievalContributorHost:
    """Run additive contributors without letting them rewrite the baseline.

    The empty-registry path deliberately returns the exact input object before
    consulting clocks, cancellation, availability, context or event sinks.
    """

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._event_sink = event_sink
        self._clock = clock
        self._registrations = registry.contributions(
            RETRIEVAL_CONTRIBUTOR_POINT
        )
        for registered in self._registrations:
            implementation = registered.contribution.implementation
            invocations = getattr(implementation, "invocations", None)
            if (
                not isinstance(invocations, frozenset)
                or not invocations
                or not invocations.issubset(_INVOCATIONS)
                or not callable(getattr(implementation, "contribute", None))
            ):
                raise ExtensionRegistryError(
                    f"retrieval contributor "
                    f"{registered.contribution.declaration.id!r} "
                    "does not implement the retrieval contributor contract"
                )

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    def run(
        self,
        baseline: Sequence[T],
        *,
        invocation: RetrievalInvocation,
        context_factory: Callable[[], RetrievalExtensionContext] | None = None,
        baseline_identity: Callable[[T], str] | None = None,
    ) -> Sequence[T]:
        registrations = tuple(
            registered
            for registered in self._registrations
            if invocation in getattr(
                registered.contribution.implementation, "invocations", ()
            )
        )
        if not registrations:
            return baseline

        # A configured contributor without a request-scoped core context is a
        # composition failure, not permission to call it with broader access.
        if context_factory is None:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="missing_retrieval_context",
                )
            return baseline

        try:
            context = context_factory()
        except Exception:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="retrieval_context_failed",
                )
            return baseline
        if context.invocation != invocation:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="retrieval_invocation_mismatch",
                )
            return baseline
        if baseline_identity is None:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="missing_baseline_identity",
                )
            return baseline

        self._raise_if_core_cancelled(context)
        try:
            connection_held = context.connection.is_connection_held()
        except Exception:
            connection_held = True
        if connection_held:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="blocked",
                    failure_code="database_connection_held",
                )
            return baseline

        try:
            item_limit = max(0, int(context.budget.max_items))
            token_limit = max(0, int(context.budget.max_tokens))
        except (TypeError, ValueError, OverflowError):
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="invalid_contribution_budget",
                )
            return baseline
        if item_limit == 0 or token_limit == 0:
            return baseline

        try:
            baseline_ids = {
                str(baseline_identity(item) or "") for item in baseline
            }
        except Exception:
            for registered in registrations:
                self._emit(
                    registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="baseline_identity_failed",
                )
            return baseline
        accepted: list[T] = []
        accepted_ids: set[str] = set()
        used_tokens = 0

        for registered in registrations:
            if len(accepted) >= item_limit:
                break
            self._raise_if_core_cancelled(context)
            contribution_id = registered.contribution.declaration.id
            try:
                availability = self._registry.availability(
                    contribution_id, context
                )
            except Exception:
                self._emit(
                    contribution_id,
                    outcome="unavailable",
                    failure_code="availability_failed",
                )
                continue
            self._raise_if_core_cancelled(context)
            if availability.status is not AvailabilityStatus.AVAILABLE:
                self._emit(
                    contribution_id,
                    outcome=availability.status.value,
                    failure_code=self._stable_code(
                        availability.reason_code, "contribution_unavailable"
                    ),
                )
                continue

            if self._deadline_expired(context):
                self._emit(
                    contribution_id,
                    outcome="timeout",
                    failure_code="contribution_timeout",
                )
                continue

            started = self._clock()
            try:
                implementation = registered.contribution.implementation
                result = implementation.contribute(context)
            except TimeoutError:
                self._emit(
                    contribution_id,
                    outcome="timeout",
                    failure_code="contribution_timeout",
                    started=started,
                )
                continue
            except Exception:
                # Recheck the core token before mapping an extension exception
                # to fail-open. A request cancellation is terminal and must be
                # propagated to the Ask/Report owner.
                self._raise_if_core_cancelled(context)
                self._emit(
                    contribution_id,
                    outcome="failed",
                    failure_code="contribution_failed",
                    started=started,
                )
                continue

            self._raise_if_core_cancelled(context)
            if self._deadline_expired(context):
                self._emit(
                    contribution_id,
                    outcome="timeout",
                    failure_code="contribution_timeout",
                    started=started,
                )
                continue
            if not self._valid_result(result):
                self._emit(
                    contribution_id,
                    outcome="invalid",
                    failure_code="invalid_contribution_result",
                    started=started,
                )
                continue
            if result.failure is not None:
                if (
                    result.failure.kind is ExtensionFailureKind.CANCELLED
                    and context.cancellation.is_set()
                ):
                    self._raise_if_core_cancelled(context)
                self._emit(
                    contribution_id,
                    outcome=result.failure.kind.value,
                    failure_code=self._stable_code(
                        result.failure.code, "contribution_failed"
                    ),
                    dropped_count=len(result.items),
                    started=started,
                )
                continue
            if result.status is ExtensionResultStatus.UNAVAILABLE:
                self._emit(
                    contribution_id,
                    outcome="unavailable",
                    failure_code="contribution_unavailable",
                    dropped_count=len(result.items),
                    started=started,
                )
                continue

            kept = 0
            dropped = 0
            structurally_valid: list[EvidenceCandidate[Any]] = []
            for candidate in result.items:
                if not self._candidate_structurally_valid(candidate):
                    dropped += 1
                    continue
                structurally_valid.append(candidate)
            try:
                scope_decisions = context.reader.allows_many(
                    tuple(structurally_valid)
                )
            except Exception:
                self._raise_if_core_cancelled(context)
                scope_decisions = ()
            self._raise_if_core_cancelled(context)
            if self._deadline_expired(context):
                self._emit(
                    contribution_id,
                    outcome="timeout",
                    failure_code="contribution_timeout",
                    dropped_count=len(result.items),
                    started=started,
                )
                continue
            if (
                not isinstance(scope_decisions, tuple)
                or len(scope_decisions) != len(structurally_valid)
                or any(
                    not isinstance(decision, bool)
                    for decision in scope_decisions
                )
            ):
                dropped += len(structurally_valid)
                structurally_valid = []
                scope_decisions = ()
            for candidate, allowed in zip(structurally_valid, scope_decisions):
                if not allowed:
                    dropped += 1
                    continue
                identity = candidate.identity
                if identity in baseline_ids or identity in accepted_ids:
                    dropped += 1
                    continue
                token_cost = max(0, int(candidate.token_cost))
                if len(accepted) >= item_limit or used_tokens + token_cost > token_limit:
                    dropped += 1
                    continue
                accepted.append(candidate.value)
                accepted_ids.add(identity)
                used_tokens += token_cost
                kept += 1
            self._emit(
                contribution_id,
                outcome=result.status.value,
                accepted_count=kept,
                dropped_count=dropped,
                started=started,
            )

        self._raise_if_core_cancelled(context)
        if not accepted:
            return baseline
        return tuple((*baseline, *accepted))

    @staticmethod
    def _valid_result(result: object) -> bool:
        return (
            hasattr(result, "items")
            and isinstance(getattr(result, "items"), tuple)
            and isinstance(getattr(result, "status", None), ExtensionResultStatus)
            and (
                getattr(result, "failure", None) is None
                or isinstance(result.failure, ExtensionFailure)
            )
        )

    @staticmethod
    def _candidate_structurally_valid(candidate: object) -> bool:
        if not isinstance(candidate, EvidenceCandidate):
            return False
        if (
            not candidate.identity
            or not candidate.notebook_id
            or not candidate.source_id
            or candidate.provenance.kind not in _PROVENANCE_KINDS
            or not candidate.provenance.reference
            or candidate.value is None
            or candidate.token_cost < 0
        ):
            return False
        return True

    @staticmethod
    def _raise_if_core_cancelled(context: RetrievalExtensionContext) -> None:
        raise_cancelled = getattr(
            context.cancellation, "raise_if_cancelled", None
        )
        if callable(raise_cancelled):
            raise_cancelled()
        elif context.cancellation.is_set():
            raise RetrievalHostCancelled("retrieval request cancelled")

    def _deadline_expired(self, context: RetrievalExtensionContext) -> bool:
        deadline = context.budget.deadline_monotonic
        return deadline is not None and self._clock() >= deadline

    @staticmethod
    def _stable_code(value: str, fallback: str) -> str:
        return value if _STABLE_CODE.fullmatch(str(value or "")) else fallback

    def _emit(
        self,
        contribution_id: str,
        *,
        outcome: str,
        accepted_count: int = 0,
        dropped_count: int = 0,
        failure_code: str = "",
        started: float | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        elapsed_ms = (
            max(0, round((self._clock() - started) * 1000))
            if started is not None
            else 0
        )
        event = RetrievalContributionEvent(
            kind="retrieval_contribution",
            contribution_id=contribution_id,
            outcome=outcome,
            accepted_count=accepted_count,
            dropped_count=dropped_count,
            elapsed_ms=elapsed_ms,
            failure_code=failure_code,
        )
        try:
            self._event_sink({
                "kind": event.kind,
                "contribution_id": event.contribution_id,
                "outcome": event.outcome,
                "accepted_count": event.accepted_count,
                "dropped_count": event.dropped_count,
                "elapsed_ms": event.elapsed_ms,
                "failure_code": event.failure_code,
            })
        except Exception:
            return
