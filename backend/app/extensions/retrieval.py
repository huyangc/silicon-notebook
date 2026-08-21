"""Baseline-preserving runner for ``retrieval.contributor`` extensions."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
import re
import time
from types import MappingProxyType
from typing import Any, TypeVar

from app.domain.extensions import (
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
)
from app.extension_sdk import (
    RETRIEVAL_CONTRIBUTOR_POINT,
    RETRIEVAL_SCOPE_READER_CAPABILITY,
    SCHEDULED_MODEL_ACCESS_CAPABILITY,
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    ActorRef,
    AvailabilityStatus,
    CancellationToken,
    ContributorResult,
    EvidenceCandidate,
    EvidenceProvenance,
    EvidenceReadRequest,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    FrozenRetrievalScopeRef,
    NotebookRef,
    RetrievalAdmissionPolicy,
    RetrievalAvailabilityContext,
    RetrievalContributionEvent,
    RetrievalContributionBudget,
    RetrievalExtensionContext,
    RetrievalHostContext,
    RetrievalInvocation,
    RetrievalRunRef,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


T = TypeVar("T")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_STABLE_METADATA_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PROVENANCE_KINDS = frozenset({
    "chunk", "element", "knowledge_object", "relation", "ppr",
})
_INVOCATIONS = frozenset({"selected_evidence", "chunk_candidates"})
_ADMISSION_POLICIES = frozenset({"additive", "atomic"})


def _candidate_from_domain(
    proposal: object,
) -> EvidenceCandidate[Any] | None:
    if type(proposal) is not RetrievalEvidenceProposal:
        return None
    try:
        return EvidenceCandidate(
            identity=proposal.identity,
            notebook_id=proposal.notebook_id,
            source_id=proposal.source_id,
            provenance=EvidenceProvenance(
                proposal.provenance_kind,
                proposal.provenance_reference,
            ),
            value=proposal.value,
            token_cost=proposal.token_cost,
        )
    except Exception:
        return None


class _CallEvidenceReader:
    def __init__(self, source: object) -> None:
        self._source = source

    def read(
        self, request: EvidenceReadRequest
    ) -> tuple[EvidenceCandidate[Any], ...]:
        raw = self._source.read(request.identities)
        if type(raw) is not tuple:
            return ()
        converted = tuple(_candidate_from_domain(item) for item in raw)
        if any(item is None for item in converted):
            return ()
        return converted  # type: ignore[return-value]


class _SelectedSourceGraphAccess:
    def __init__(self, source: object) -> None:
        self._source = source

    def contribute(self) -> ContributorResult[EvidenceCandidate[Any]]:
        raw = self._source.propose()
        if type(raw) is not tuple:
            return ContributorResult(
                (),
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.INVALID_RESULT,
                    "invalid_core_proposal",
                ),
            )
        converted = tuple(_candidate_from_domain(item) for item in raw)
        if any(item is None for item in converted):
            return ContributorResult(
                (),
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.INVALID_RESULT,
                    "invalid_core_proposal",
                ),
            )
        return ContributorResult(
            converted,  # type: ignore[arg-type]
            ExtensionResultStatus.AVAILABLE,
        )


class RetrievalHostCancelled(RuntimeError):
    """Core request cancellation; callers must propagate, never fail open."""


@dataclass(frozen=True)
class _FrozenRegistration:
    registered: RegisteredContribution
    invocations: frozenset[RetrievalInvocation]
    requires: frozenset[str]
    optional_requires: frozenset[str]
    admission: RetrievalAdmissionPolicy


class RetrievalContributorHost:
    """Run governed contributors without letting them rewrite the baseline."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        admission_policies: Mapping[str, RetrievalAdmissionPolicy] | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_sink = event_sink
        self._clock = clock
        policies = dict(admission_policies or {})
        registered = registry.contributions(RETRIEVAL_CONTRIBUTOR_POINT)
        known_ids = {item.contribution.declaration.id for item in registered}
        unknown = sorted(set(policies) - known_ids)
        if unknown:
            raise ExtensionRegistryError(
                f"admission policies name unknown contributions {unknown!r}"
            )
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        frozen: list[_FrozenRegistration] = []
        by_invocation: dict[str, list[_FrozenRegistration]] = {
            invocation: [] for invocation in _INVOCATIONS
        }
        for item in registered:
            implementation = item.contribution.implementation
            invocations = getattr(implementation, "invocations", None)
            if (
                not isinstance(invocations, frozenset)
                or not invocations
                or not invocations.issubset(_INVOCATIONS)
                or not callable(getattr(implementation, "contribute", None))
            ):
                raise ExtensionRegistryError(
                    f"retrieval contributor {item.contribution.declaration.id!r} "
                    "does not implement the retrieval contributor contract"
                )
            contribution_id = item.contribution.declaration.id
            admission = policies.get(contribution_id, "additive")
            if admission not in _ADMISSION_POLICIES:
                raise ExtensionRegistryError(
                    f"retrieval contributor {contribution_id!r} has invalid "
                    "core admission policy"
                )
            manifest = manifests[item.plugin_id]
            snapshot = _FrozenRegistration(
                registered=item,
                invocations=frozenset(invocations),
                requires=frozenset(manifest.requires),
                optional_requires=frozenset(manifest.optional_requires),
                admission=admission,
            )
            frozen.append(snapshot)
            for invocation in snapshot.invocations:
                by_invocation[invocation].append(snapshot)
        self._registrations = tuple(frozen)
        self._by_invocation = MappingProxyType({
            invocation: tuple(items)
            for invocation, items in by_invocation.items()
        })
        self._registry = registry

    def run(
        self,
        baseline: Sequence[T],
        *,
        invocation: RetrievalInvocation,
        context_factory: Callable[[], RetrievalHostContext] | None = None,
        call_context: RetrievalContributionCallContext | None = None,
        baseline_identity: Callable[[T], str] | None = None,
        cancellation: CancellationToken | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> Sequence[T]:
        registrations = self._by_invocation.get(invocation, ())
        if not registrations:
            return baseline

        if (
            cancellation is not None
            and not self._valid_cancellation_token(cancellation)
        ):
            for registration in registrations:
                self._emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="invalid_cancellation_token",
                    event_sink=event_sink,
                )
            return baseline

        if context_factory is not None and call_context is not None:
            context_factory = None
        elif call_context is not None:
            context_factory = lambda: self._context_from_call(
                invocation, call_context
            )

        available: list[_FrozenRegistration] = []
        for registration in registrations:
            if cancellation is not None:
                self._raise_if_token_cancelled(cancellation)
            contribution_id = registration.registered.contribution.declaration.id
            availability = self._registry.contribution_availability(
                contribution_id,
                RetrievalAvailabilityContext(
                    plugin_id=registration.registered.plugin_id,
                    contribution_id=contribution_id,
                    invocation=invocation,
                    cancellation=cancellation,
                ),
            )
            if cancellation is not None:
                self._raise_if_token_cancelled(cancellation)
            if availability.status is not AvailabilityStatus.AVAILABLE:
                self._emit(
                    contribution_id,
                    outcome=availability.status.value,
                    failure_code=self._stable_code(
                        availability.reason_code, "contribution_unavailable"
                    ),
                    event_sink=event_sink,
                )
                continue
            available.append(registration)
        if not available:
            return baseline

        if context_factory is None:
            for registration in available:
                self._emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="missing_retrieval_context",
                    event_sink=event_sink,
                )
            return baseline
        try:
            context = context_factory()
        except Exception:
            if cancellation is not None:
                self._raise_if_token_cancelled(cancellation)
            for registration in available:
                self._emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="retrieval_context_failed",
                    event_sink=event_sink,
                )
            return baseline
        if cancellation is not None:
            self._raise_if_token_cancelled(cancellation)

        def emit(contribution_id, **kwargs):
            self._emit(
                contribution_id,
                event_sink=event_sink,
                **kwargs,
            )

        if type(context) is not RetrievalHostContext:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="retrieval_invocation_mismatch",
                )
            return baseline
        self._raise_if_core_cancelled(context)
        if context.invocation != invocation:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="retrieval_invocation_mismatch",
                )
            return baseline
        if baseline_identity is None:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="missing_baseline_identity",
                )
            return baseline

        try:
            connection_held = context.connection.is_connection_held()
        except Exception:
            self._raise_if_core_cancelled(context)
            connection_held = None
        self._raise_if_core_cancelled(context)
        if type(connection_held) is not bool:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="invalid_connection_probe",
                )
            return baseline
        if connection_held:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="blocked",
                    failure_code="database_connection_held",
                )
            return baseline

        budget = self._validated_budget(context)
        if budget is None:
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="invalid_contribution_budget",
                )
            return baseline
        item_limit, token_limit, proposal_limit = budget
        if item_limit == 0 or token_limit == 0 or proposal_limit == 0:
            return baseline
        try:
            baseline_ids: set[str] = set()
            for item in baseline:
                identity = baseline_identity(item)
                self._raise_if_core_cancelled(context)
                if type(identity) is not str or not identity:
                    raise ValueError("invalid baseline identity")
                baseline_ids.add(identity)
        except Exception:
            self._raise_if_core_cancelled(context)
            for registration in available:
                emit(
                    registration.registered.contribution.declaration.id,
                    outcome="invalid_context",
                    failure_code="baseline_identity_failed",
                )
            return baseline

        accepted: list[T] = []
        accepted_ids: set[str] = set()
        used_tokens = 0
        for registration in available:
            if len(accepted) >= item_limit:
                break
            self._raise_if_core_cancelled(context)
            contribution_id = registration.registered.contribution.declaration.id
            granted = self._granted_capabilities(registration, context)
            self._raise_if_core_cancelled(context)
            if granted is None:
                emit(
                    contribution_id,
                    outcome="unavailable",
                    failure_code="required_capability_unavailable",
                )
                continue
            if self._deadline_expired(context):
                emit(
                    contribution_id,
                    outcome="timeout",
                    failure_code="contribution_timeout",
                )
                continue
            plugin_context = RetrievalExtensionContext(
                invocation=context.invocation,
                actor=context.actor,
                notebook=context.notebook,
                scope=context.scope,
                run=context.run,
                cancellation=context.cancellation,
                budget=context.budget,
                reader=(
                    context.admission_reader
                    if RETRIEVAL_SCOPE_READER_CAPABILITY in granted
                    else None
                ),
                models=(
                    context.model_access
                    if SCHEDULED_MODEL_ACCESS_CAPABILITY in granted
                    else None
                ),
                selected_source_graph=(
                    context.selected_source_graph_access
                    if SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY in granted
                    else None
                ),
            )
            started = self._clock()
            try:
                result = registration.registered.contribution.implementation.contribute(
                    plugin_context
                )
            except TimeoutError:
                emit(
                    contribution_id, outcome="timeout",
                    failure_code="contribution_timeout", started=started,
                )
                continue
            except Exception:
                self._raise_if_core_cancelled(context)
                emit(
                    contribution_id, outcome="failed",
                    failure_code="contribution_failed", started=started,
                )
                continue

            self._raise_if_core_cancelled(context)
            if self._deadline_expired(context):
                emit(
                    contribution_id, outcome="timeout",
                    failure_code="contribution_timeout", started=started,
                )
                continue
            if not self._valid_result(result):
                emit(
                    contribution_id, outcome="invalid",
                    failure_code="invalid_contribution_result", started=started,
                )
                continue
            if result.failure is not None:
                if (
                    result.failure.kind is ExtensionFailureKind.CANCELLED
                    and context.cancellation.is_set()
                ):
                    self._raise_if_core_cancelled(context)
                emit(
                    contribution_id, outcome=result.failure.kind.value,
                    failure_code=self._stable_code(
                        result.failure.code, "contribution_failed"
                    ),
                    dropped_count=len(result.items), started=started,
                )
                continue
            if result.status is ExtensionResultStatus.UNAVAILABLE:
                emit(
                    contribution_id, outcome="unavailable",
                    failure_code="contribution_unavailable",
                    dropped_count=len(result.items), started=started,
                )
                continue

            proposals = result.items[:proposal_limit]
            dropped = len(result.items) - len(proposals)
            valid: list[EvidenceCandidate[Any]] = []
            for candidate in proposals:
                if self._candidate_structurally_valid(candidate):
                    valid.append(candidate)
                else:
                    dropped += 1
            if registration.admission == "atomic" and dropped:
                emit(
                    contribution_id, outcome="rejected",
                    dropped_count=len(result.items),
                    failure_code="atomic_admission_rejected", started=started,
                )
                continue
            try:
                hydrated = context.admission_reader.read(
                    EvidenceReadRequest(tuple(item.identity for item in valid))
                )
            except Exception:
                self._raise_if_core_cancelled(context)
                hydrated = ()
            self._raise_if_core_cancelled(context)
            if self._deadline_expired(context):
                emit(
                    contribution_id, outcome="timeout",
                    failure_code="contribution_timeout",
                    dropped_count=len(result.items), started=started,
                )
                continue
            authoritative = self._authoritative_by_identity(
                hydrated, expected_limit=len(valid)
            )
            if authoritative is None:
                dropped += len(valid)
                valid = []
                authoritative = {}

            pending: list[EvidenceCandidate[Any]] = []
            pending_ids: set[str] = set()
            pending_tokens = 0
            for proposal in valid:
                candidate = authoritative.get(proposal.identity)
                if (
                    candidate is None
                    or not self._same_authority(proposal, candidate)
                    or candidate.identity in baseline_ids
                    or candidate.identity in accepted_ids
                    or candidate.identity in pending_ids
                    or len(accepted) + len(pending) >= item_limit
                    or used_tokens + pending_tokens + candidate.token_cost > token_limit
                ):
                    dropped += 1
                    continue
                pending.append(candidate)
                pending_ids.add(candidate.identity)
                pending_tokens += candidate.token_cost
            if registration.admission == "atomic" and dropped:
                emit(
                    contribution_id, outcome="rejected",
                    dropped_count=len(result.items),
                    failure_code="atomic_admission_rejected", started=started,
                )
                continue
            accepted.extend(candidate.value for candidate in pending)
            accepted_ids.update(pending_ids)
            used_tokens += pending_tokens
            emit(
                contribution_id, outcome=result.status.value,
                accepted_count=len(pending), dropped_count=dropped, started=started,
            )

        self._raise_if_core_cancelled(context)
        if not accepted:
            return baseline
        return tuple((*baseline, *accepted))

    @staticmethod
    def _context_from_call(
        invocation: RetrievalInvocation,
        call: RetrievalContributionCallContext,
    ) -> RetrievalHostContext | None:
        if (
            type(call) is not RetrievalContributionCallContext
            or type(call.actor_id) is not str
            or type(call.notebook_id) is not str
            or not call.notebook_id
            or type(call.scope_id) is not str
            or not call.scope_id
            or type(call.scope_narrowed) is not bool
            or type(call.run_id) is not str
            or type(call.run_kind) is not str
            or not RetrievalContributorHost._valid_cancellation_token(
                call.cancellation
            )
            or not callable(getattr(call.proposal_source, "propose", None))
            or not callable(getattr(call.proposal_source, "read", None))
            or not callable(
                getattr(call.connection_probe, "is_connection_held", None)
            )
            or type(call.selected_source_graph_available) is not bool
        ):
            return None
        access = _SelectedSourceGraphAccess(call.proposal_source)
        return RetrievalHostContext(
            invocation=invocation,
            actor=ActorRef(call.actor_id),
            notebook=NotebookRef(call.notebook_id),
            scope=FrozenRetrievalScopeRef(call.scope_id, call.scope_narrowed),
            run=RetrievalRunRef(call.run_id, call.run_kind),
            cancellation=call.cancellation,
            budget=RetrievalContributionBudget(
                call.max_items,
                call.max_tokens,
                call.max_proposals,
                call.deadline_monotonic,
            ),
            admission_reader=_CallEvidenceReader(call.proposal_source),
            model_access=None,
            selected_source_graph_access=(
                access if call.selected_source_graph_available else None
            ),
            connection=call.connection_probe,
        )

    @staticmethod
    def _valid_cancellation_token(cancellation: object) -> bool:
        try:
            is_set = getattr(cancellation, "is_set", None)
            raise_cancelled = getattr(cancellation, "raise_if_cancelled", None)
        except Exception:
            return False
        return callable(is_set) and (
            raise_cancelled is None or callable(raise_cancelled)
        )

    def _granted_capabilities(
        self, registration: _FrozenRegistration, context: RetrievalHostContext,
    ) -> frozenset[str] | None:
        granted: set[str] = set()
        for capability in sorted(
            registration.requires | registration.optional_requires
        ):
            decision = self._registry.capability_availability(capability, context)
            self._raise_if_core_cancelled(context)
            if not isinstance(decision.status, AvailabilityStatus):
                if capability in registration.requires:
                    return None
                continue
            if decision.status is AvailabilityStatus.AVAILABLE:
                granted.add(capability)
            elif capability in registration.requires:
                return None
        return frozenset(granted)

    @staticmethod
    def _validated_budget(
        context: RetrievalHostContext,
    ) -> tuple[int, int, int] | None:
        values = (
            context.budget.max_items,
            context.budget.max_tokens,
            context.budget.max_proposals,
        )
        if any(
            type(value) is not int or value < 0
            for value in values
        ):
            return None
        deadline = context.budget.deadline_monotonic
        if (
            deadline is not None
            and (
                type(deadline) not in (int, float)
                or not math.isfinite(deadline)
            )
        ):
            return None
        return values

    @staticmethod
    def _valid_result(result: object) -> bool:
        try:
            failure = getattr(result, "failure", None)
            return (
                type(result) is ContributorResult
                and type(getattr(result, "items", None)) is tuple
                and isinstance(getattr(result, "status", None), ExtensionResultStatus)
                and (
                    failure is None
                    or (
                        type(failure) is ExtensionFailure
                        and isinstance(failure.kind, ExtensionFailureKind)
                        and type(failure.code) is str
                    )
                )
            )
        except Exception:
            return False

    @staticmethod
    def _candidate_structurally_valid(candidate: object) -> bool:
        try:
            return (
                type(candidate) is EvidenceCandidate
                and type(candidate.identity) is str
                and bool(candidate.identity)
                and type(candidate.notebook_id) is str
                and bool(candidate.notebook_id)
                and type(candidate.source_id) is str
                and bool(candidate.source_id)
                and type(candidate.provenance) is EvidenceProvenance
                and type(candidate.provenance.kind) is str
                and candidate.provenance.kind in _PROVENANCE_KINDS
                and type(candidate.provenance.reference) is str
                and bool(candidate.provenance.reference)
                and candidate.value is not None
                and type(candidate.token_cost) is int
                and candidate.token_cost >= 0
            )
        except Exception:
            return False

    @classmethod
    def _authoritative_by_identity(
        cls, candidates: object, *, expected_limit: int,
    ) -> dict[str, EvidenceCandidate[Any]] | None:
        if type(candidates) is not tuple or len(candidates) > expected_limit:
            return None
        result: dict[str, EvidenceCandidate[Any]] = {}
        for candidate in candidates:
            if (
                not cls._candidate_structurally_valid(candidate)
                or candidate.identity in result
            ):
                return None
            result[candidate.identity] = candidate
        return result

    @staticmethod
    def _same_authority(
        proposal: EvidenceCandidate[Any], authoritative: EvidenceCandidate[Any],
    ) -> bool:
        return (
            proposal.identity == authoritative.identity
            and proposal.notebook_id == authoritative.notebook_id
            and proposal.source_id == authoritative.source_id
            and proposal.provenance == authoritative.provenance
        )

    @staticmethod
    def _raise_if_core_cancelled(context: RetrievalHostContext) -> None:
        RetrievalContributorHost._raise_if_token_cancelled(context.cancellation)

    @staticmethod
    def _raise_if_token_cancelled(cancellation: object) -> None:
        raise_cancelled = getattr(cancellation, "raise_if_cancelled", None)
        if callable(raise_cancelled):
            raise_cancelled()
        elif cancellation.is_set():
            raise RetrievalHostCancelled("retrieval request cancelled")

    def _deadline_expired(self, context: RetrievalHostContext) -> bool:
        deadline = context.budget.deadline_monotonic
        return deadline is not None and self._clock() >= deadline

    @staticmethod
    def _stable_code(value: object, fallback: str) -> str:
        return (
            value
            if type(value) is str and _STABLE_CODE.fullmatch(value)
            else fallback
        )

    def _emit(
        self, contribution_id: str, *, outcome: str,
        accepted_count: int = 0, dropped_count: int = 0,
        failure_code: str = "", started: float | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if (
            type(contribution_id) is not str
            or not _STABLE_METADATA_ID.fullmatch(contribution_id)
        ):
            contribution_id = "invalid_contribution"
        sink = event_sink if event_sink is not None else self._event_sink
        if sink is None:
            return
        elapsed_ms = (
            max(0, round((self._clock() - started) * 1000))
            if started is not None else 0
        )
        event = RetrievalContributionEvent(
            kind="retrieval_contribution", contribution_id=contribution_id,
            outcome=outcome, accepted_count=accepted_count,
            dropped_count=dropped_count, elapsed_ms=elapsed_ms,
            failure_code=failure_code,
        )
        try:
            sink({
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
