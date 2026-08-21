"""Stable application ports for consuming extension hosts without a registry."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, TypeVar


RetrievalInvocation = Literal["selected_evidence", "chunk_candidates"]
T = TypeVar("T")


class RetrievalContributorHostPort(Protocol):
    def run(
        self,
        baseline: Sequence[T],
        *,
        invocation: RetrievalInvocation,
        context_factory: Callable[[], Any] | None = None,
        baseline_identity: Callable[[T], str] | None = None,
        cancellation: Any | None = None,
    ) -> Sequence[T]: ...
