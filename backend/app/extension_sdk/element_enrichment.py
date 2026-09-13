"""Point-specific contract for optional parsed-element enrichment.

A contributor at this point sees core's own read-only projection of the
elements a parser produced — never the parser's raw metadata mapping, never a
repository, never a settings object.  It may propose additional structured
metadata and one retrievable ``description`` per element; it may not rewrite
anything core already parsed, ``caption`` included.

Image bytes are the one payload that does not travel inside the projection.
They are read on demand through :class:`ElementAssetReader`, which is bound to
one ``enrich`` call: core resolves the on-disk locations on the calling thread
*before* any plugin runs, so the contributor's own thread never touches the
database, and the reader answers ``None`` for everything once the call the
reader was issued for has ended.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.extension_sdk.contracts import CancellationToken, ContributorResult


SOURCE_ELEMENT_ENRICHER_POINT = "source.element_enricher"


@dataclass(frozen=True, slots=True)
class ElementRef:
    """Opaque request-local authority; candidates must return this exact object.

    Identity is the whole point: core matches a candidate's ``element`` against
    the refs it minted for *this* call with ``is``, so a plugin cannot address
    an element it was not shown by reconstructing an equal-looking value.
    """

    token: object


@dataclass(frozen=True, slots=True)
class ElementView:
    """Core-owned read-only projection of one parsed element.

    ``asset_id`` is non-empty exactly when this element carries an image core
    has already persisted; ``asset_mime`` is then its media type.  The bytes
    are fetched through :class:`ElementAssetReader`, never carried here.
    """

    ref: ElementRef
    element_type: str
    location_label: str
    text: str
    caption: str = ""
    description: str = ""
    asset_id: str = ""
    asset_mime: str = ""


@runtime_checkable
class ElementAssetReader(Protocol):
    """Bounded, call-scoped access to one element's already-persisted image.

    Returns the element's image bytes, or ``None`` when it has no image, when
    the image exceeds ``ElementEnrichmentBudget.max_asset_bytes``, when the
    read fails, when ``ref`` was not issued for this call, or when the call
    this reader was issued for has already returned.  It never raises.
    """

    def read(self, ref: ElementRef) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class ElementEnrichmentBudget:
    """Everything this contribution is allowed to spend on this call.

    ``max_proposals`` is what the *point* can still accept, so it may already
    be smaller than the deployment's configured maximum by the time a later
    contributor is reached.  ``max_metadata_bytes`` is per contribution: the
    total persisted size of the ``extensions`` subtree this one contribution
    would add, descriptions included.
    """

    max_proposals: int
    max_metadata_bytes: int
    max_description_chars: int
    max_asset_bytes: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ElementEnrichmentAvailabilityContext:
    """I/O-free live availability input, mirroring the gap-consult one."""

    plugin_id: str
    contribution_id: str
    element_count: int
    image_count: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ElementEnrichmentContext:
    """Per-contribution projection of one parsed source.

    Every parsed element is offered, not just the images: which ones are worth
    looking at is the plugin's policy, expressed by filtering on
    ``element_type``/``asset_id``.  There is deliberately no repository,
    settings, or connection port here.
    """

    elements: tuple[ElementView, ...]
    assets: ElementAssetReader
    cancellation: CancellationToken
    budget: ElementEnrichmentBudget


@dataclass(frozen=True, slots=True)
class ElementEnrichmentCandidate:
    """One element's proposed enrichment.

    ``metadata`` keys match ``^[a-z][a-z0-9_]{0,63}$`` and values are strict
    JSON scalars, sequences or mappings nested at most
    ``ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH`` deep; non-finite floats are
    rejected.  ``description`` is admitted as retrievable text and may carry
    fenced code blocks.  ``caption`` is core's and cannot be proposed.
    """

    element: ElementRef
    metadata: Mapping[str, Any]
    description: str = ""


@runtime_checkable
class ElementEnricher(Protocol):
    """Propose enrichment for elements of one freshly parsed source.

    At most one candidate per element per contribution.  An ``UNAVAILABLE``
    result is discarded whole — the contributor's own statement that it could
    not serve this call — while ``PARTIAL`` is admitted like ``AVAILABLE``.
    Any shape violation discards this contribution's whole batch and leaves
    every other contribution untouched.
    """

    def enrich(
        self, context: ElementEnrichmentContext
    ) -> ContributorResult[ElementEnrichmentCandidate]: ...


__all__ = [
    "SOURCE_ELEMENT_ENRICHER_POINT",
    "ElementAssetReader",
    "ElementEnricher",
    "ElementEnrichmentAvailabilityContext",
    "ElementEnrichmentBudget",
    "ElementEnrichmentCandidate",
    "ElementEnrichmentContext",
    "ElementRef",
    "ElementView",
]
