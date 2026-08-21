"""Stable application ports for consuming extension hosts without a registry."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar


RetrievalInvocation = Literal["selected_evidence", "chunk_candidates"]
T = TypeVar("T")


@dataclass(frozen=True)
class RetrievalEvidenceProposal:
    """Core-authored proposal shape consumed by a built-in adapter.

    This lives in ``domain`` so application services can supply authoritative
    evidence without importing the Extension SDK or registry runtime.
    """

    identity: str
    notebook_id: str
    source_id: str
    provenance_kind: str
    provenance_reference: str
    value: Any
    token_cost: int


class RetrievalProposalSourcePort(Protocol):
    """One request-local source behind a narrow built-in capability."""

    def propose(self) -> tuple[RetrievalEvidenceProposal, ...]: ...

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]: ...


@dataclass(frozen=True)
class RetrievalContributionCallContext:
    """Core-only inputs from a workflow to the shared contributor host."""

    actor_id: str
    notebook_id: str
    scope_id: str
    scope_narrowed: bool
    run_id: str
    run_kind: str
    cancellation: Any
    max_items: int
    max_tokens: int
    max_proposals: int
    proposal_source: RetrievalProposalSourcePort
    connection_probe: Any
    deadline_monotonic: float | None = None


class RetrievalContributorHostPort(Protocol):
    def run(
        self,
        baseline: Sequence[T],
        *,
        invocation: RetrievalInvocation,
        context_factory: Callable[[], Any] | None = None,
        call_context: RetrievalContributionCallContext | None = None,
        baseline_identity: Callable[[T], str] | None = None,
        cancellation: Any | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> Sequence[T]: ...
