"""Strict source scopes for reasoning evidence retrieval.

This module is deliberately independent from Ask orchestration.  It owns the
one important distinction used by every later adapter: ``None`` means all
authorized sources, while an explicitly selected scope is non-empty and can
only become narrower.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Literal, Protocol, Sequence

from app.services.retrieval import (
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.services.source_display import source_display_title


ScopeResolutionStatus = Literal["not_found", "ambiguous", "unavailable"]


class SourceIdentityRow(Protocol):
    def __getitem__(self, key: str) -> object: ...


def _identity_key(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


@dataclass(frozen=True)
class SourceIdentity:
    """One authorized, user-visible source identity available to the resolver."""

    source_id: str
    notebook_id: str
    title: str
    source_file_name: str = ""

    def __post_init__(self) -> None:
        if not self.source_id or not self.notebook_id:
            raise ValueError("a source identity requires source and notebook ids")


@dataclass(frozen=True)
class SourceIdentitySnapshot:
    """Display-safe identity frozen for trace and persisted response output."""

    source_id: str
    notebook_id: str
    title: str
    source_file_name: str = ""

    def __post_init__(self) -> None:
        if not self.source_id or not self.notebook_id:
            raise ValueError("a source snapshot requires source and notebook ids")


@dataclass(frozen=True)
class ResolvedEvidenceScope:
    """Immutable run-local source ceiling.

    ``restricted=False`` is the only all-source representation.  A restricted
    scope can never be empty, so repository code never has to guess whether an
    empty collection means "none" or "all".
    """

    restricted: bool
    sources: tuple[SourceIdentitySnapshot, ...] = ()

    def __post_init__(self) -> None:
        if self.restricted and not self.sources:
            raise ValueError("a selected evidence scope must contain a source")
        if not self.restricted and self.sources:
            raise ValueError("an all-source evidence scope cannot carry sources")
        keys = [(item.notebook_id, item.source_id) for item in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("an evidence scope cannot contain duplicate sources")

    @classmethod
    def all(cls) -> "ResolvedEvidenceScope":
        return cls(restricted=False)

    @classmethod
    def selected(
        cls, sources: Iterable[SourceIdentitySnapshot]
    ) -> "ResolvedEvidenceScope":
        return cls(restricted=True, sources=tuple(sources))

    @property
    def allowed_source_ids(self) -> frozenset[str]:
        return frozenset(item.source_id for item in self.sources)

    @property
    def allowed_source_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (item.notebook_id, item.source_id) for item in self.sources
        )

    def narrow_to(self, source_ids: Iterable[str]) -> "ResolvedEvidenceScope":
        """Return a subset of this scope; broadening is rejected."""

        if not self.restricted:
            raise ValueError("resolve an authorized selected scope before narrowing")
        requested = frozenset(str(value) for value in source_ids if str(value))
        if not requested:
            raise ValueError("a selected evidence scope cannot be empty")
        if not requested <= self.allowed_source_ids:
            raise ValueError("an evidence scope cannot be broadened")
        return ResolvedEvidenceScope.selected(
            item for item in self.sources if item.source_id in requested
        )


class EvidenceScopeResolutionError(ValueError):
    """A selected scope could not be proven uniquely and safely."""

    def __init__(
        self,
        status: ScopeResolutionStatus,
        reference: str,
        *,
        candidates: Sequence[SourceIdentitySnapshot] = (),
    ) -> None:
        super().__init__(f"source reference is {status}: {reference!r}")
        self.status = status
        self.reference = reference
        self.candidates = tuple(candidates)


def source_identity_from_row(row: SourceIdentityRow) -> SourceIdentity:
    """Create an identity using the product's single display-title rule."""

    def value(key: str) -> object:
        # ``source_display_rows`` returns sqlite3.Row locally and driver rows
        # on PostgreSQL; neither is required to implement Mapping.get().
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return ""

    return SourceIdentity(
        source_id=str(value("id") or ""),
        notebook_id=str(value("notebook_id") or ""),
        title=source_display_title(row),
        source_file_name=str(value("file_name") or ""),
    )


def resolve_evidence_scope(
    source_refs: Sequence[str] | None,
    authorized_sources: Sequence[SourceIdentity],
    *,
    catalog_truncated: bool = False,
) -> ResolvedEvidenceScope:
    """Resolve optional model references inside an already authorized catalog.

    The caller owns the bounded catalog query and authorization check.  This
    function never guesses: exact normalized id/title/file-name matching only.
    A truncated catalog cannot prove uniqueness and therefore fails closed.
    """

    if source_refs is None:
        return ResolvedEvidenceScope.all()
    if not source_refs or not any(_identity_key(value) for value in source_refs):
        raise ValueError("source_refs must be omitted or contain a source reference")
    if catalog_truncated:
        raise EvidenceScopeResolutionError("unavailable", source_refs[0])

    resolved: list[SourceIdentitySnapshot] = []
    seen: set[tuple[str, str]] = set()
    for raw_ref in source_refs:
        wanted = _identity_key(raw_ref)
        if not wanted:
            raise ValueError("source_refs cannot contain an empty reference")
        matches: list[SourceIdentity] = []
        for item in authorized_sources:
            identities = {
                _identity_key(item.source_id),
                _identity_key(item.title),
                _identity_key(item.source_file_name),
            }
            identities.discard("")
            if wanted in identities:
                matches.append(item)
        unique = {
            (item.notebook_id, item.source_id): item for item in matches
        }
        snapshots = tuple(
            SourceIdentitySnapshot(
                source_id=item.source_id,
                notebook_id=item.notebook_id,
                title=item.title,
                source_file_name=item.source_file_name,
            )
            for item in unique.values()
        )
        if not snapshots:
            raise EvidenceScopeResolutionError("not_found", str(raw_ref))
        if len(snapshots) > 1:
            raise EvidenceScopeResolutionError(
                "ambiguous", str(raw_ref), candidates=snapshots
            )
        snapshot = snapshots[0]
        key = (snapshot.notebook_id, snapshot.source_id)
        if key not in seen:
            resolved.append(snapshot)
            seen.add(key)
    return ResolvedEvidenceScope.selected(resolved)


def restrict_evidence_candidates(
    scope: ResolvedEvidenceScope,
    kind: Literal["knowledge", "element", "chunk"],
    items: Sequence[RetrievedKnowledge | RetrievedElement | RetrievedChunk],
) -> list[RetrievedKnowledge | RetrievedElement | RetrievedChunk]:
    """Apply the final run-level source defense without mutating candidates."""

    if kind == "element" or kind == "chunk":
        expected = RetrievedElement if kind == "element" else RetrievedChunk
        if any(not isinstance(item, expected) for item in items):
            raise ValueError(f"{kind} evidence contains an unexpected candidate type")
        if not scope.restricted:
            return list(items)
        allowed = scope.allowed_source_ids
        return [item for item in items if item.source_id in allowed]
    if kind != "knowledge":
        raise ValueError(f"unsupported evidence kind: {kind!r}")
    if any(not isinstance(item, RetrievedKnowledge) for item in items):
        raise ValueError("knowledge evidence contains an unexpected candidate type")
    if not scope.restricted:
        return list(items)
    allowed = scope.allowed_source_ids

    restricted: list[RetrievedKnowledge] = []
    for item in items:
        kept = [ev for ev in item.evidence if ev.source_id in allowed]
        if not kept:
            continue
        restricted.append(replace(item, evidence=kept))
    return restricted
