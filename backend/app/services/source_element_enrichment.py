"""Compose ``source.element_enricher`` patches back onto parsed elements.

This is the service-layer half of the point: the extension host decides what a
contribution is *allowed* to propose (element identity, metadata shape,
description characters, counts and persisted bytes), and this module decides
what is actually *written* onto the elements the ingestion pipeline is about to
persist.  It imports the domain ports only — never the Extension SDK, never
the registry runtime — so the ingestion service stays composable with no host
at all, which is exactly what a library or narrow-test construction passes.

Three properties are load-bearing:

* **All of it or none of it.**  Any violation anywhere in the batch returns the
  caller's original elements untouched.  A partially applied batch would leave
  provenance in a notebook claiming more than the plugin produced, and the
  elements are on their way into the same transaction as the chunk generation,
  so "half enriched" is not a state worth persisting.
* **Fail-open, never fail-loud.**  A raising host, a malformed patch, a broken
  clock: the source is ingested exactly as it parsed.  The one exception is
  ``CoreCancellation``, which is the parse job being torn down and must
  propagate rather than be swallowed into "enriched nothing".
* **Silent is not the same as clean.**  Failing open on a whole batch used to
  be indistinguishable, from outside, from a plugin that simply proposed
  nothing — an operator chasing "why is nothing being enriched" had no signal
  at all.  Every rejection therefore returns a stable reason code beside the
  unchanged elements, which the ingestion service records on its pipeline
  stage.  The empty string means "not rejected".
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import time
from typing import Callable

from app.domain.cancellation import CoreCancellation
from app.domain.element_enrichment import (
    persisted_element_enrichment_size,
    valid_element_enrichment_owner,
)
from app.domain.extensions import (
    ElementAssetLocation,
    ElementEnricherHostPort,
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ParsedElementEnvelope,
)
from app.models.sources import SourceElement


# Why a whole batch was refused.  Stable codes, never a message, an id, a path
# or a settings value: they are emitted on the parse pipeline's own event
# stream, which an operator reads and a plugin author never authors.
REJECT_INVALID_PATCH = "invalid_patch"
REJECT_INVALID_OWNER = "invalid_owner"
REJECT_INVALID_DESCRIPTION = "invalid_description"
REJECT_ORDINAL_OUT_OF_RANGE = "ordinal_out_of_range"
REJECT_DUPLICATE_CONTRIBUTION = "duplicate_contribution"
REJECT_BUDGET_EXCEEDED = "budget_exceeded"
# Not the plugin's fault, but just as invisible without a code: the caller
# passed budgets this adapter cannot honour, an element this adapter cannot
# project, or the host itself raised on the way out.
REJECT_INVALID_BUDGET = "invalid_budget"
REJECT_INVALID_ELEMENT = "invalid_element"
REJECT_HOST_FAILED = "host_failed"
#: Element types ``parsers._element`` stores with their structure intact
#: rather than whitespace-flattened.  Appending to one of these has to keep
#: that promise — see ``_appended_text``.
_STRUCTURED_TEXT_TYPES = frozenset({"code_block", "table"})


class _NeverCancelled:
    """The parse job has no cancellation token, and this says so honestly.

    The SDK contract still hands contributors a full token face, so the host
    needs something with both methods rather than ``None``; answering "not
    cancelled" is the truth here, not a stub.
    """

    __slots__ = ()

    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


def enrich_source_elements(
    elements: list[SourceElement],
    *,
    host: ElementEnricherHostPort | None,
    asset_locations: Mapping[str, ElementAssetLocation],
    connection_probe: object,
    max_proposals: int,
    max_metadata_bytes: int,
    max_description_chars: int,
    max_asset_bytes: int,
    timeout_seconds: float,
    event_sink: Callable[[dict[str, object]], None] | None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[SourceElement], str]:
    """Apply every admitted patch, or refuse the batch and say why.

    Returns ``(elements, reason_code)``.  ``reason_code`` is ``""`` when
    nothing was refused — which covers both "patches were applied" and "the
    point had nothing to say" — and one of the ``REJECT_*`` codes above when
    the whole batch was discarded.  The elements returned alongside a non-empty
    code are the caller's own list object, unchanged.
    """

    if host is None or not elements:
        return elements, ""
    if not _valid_budgets(
        max_proposals,
        max_metadata_bytes,
        max_description_chars,
        max_asset_bytes,
        timeout_seconds,
    ):
        return elements, REJECT_INVALID_BUDGET
    try:
        # Read strictly: a host that answers anything other than the literal
        # ``True`` has not said it has contributions, and the zero-cost path
        # is the one a deployment without enrichers must keep.
        if host.has_contributions() is not True:
            return elements, ""
        now = _now(clock)
        if now is None:
            return elements, REJECT_INVALID_BUDGET
        envelopes = _envelopes(elements, asset_locations)
        if envelopes is None:
            return elements, REJECT_INVALID_ELEMENT
        patches = host.enrich_application(
            ElementEnrichmentCallContext(
                envelopes,
                asset_locations,
                _NeverCancelled(),
                connection_probe,
                max_proposals,
                max_metadata_bytes,
                max_description_chars,
                max_asset_bytes,
                now + float(timeout_seconds),
            ),
            event_sink=event_sink,
        )
    except CoreCancellation:
        raise
    except Exception:  # noqa: BLE001 — a faulty host ingests the source as-is
        return elements, REJECT_HOST_FAILED
    if type(patches) is not tuple:
        return elements, REJECT_INVALID_PATCH
    if not patches:
        return elements, ""
    if len(patches) > max_proposals:
        return elements, REJECT_BUDGET_EXCEEDED
    try:
        return _applied(elements, patches, max_metadata_bytes, max_description_chars)
    except CoreCancellation:
        raise
    except Exception:  # noqa: BLE001 — composition is all-or-nothing
        return elements, REJECT_INVALID_PATCH


def _valid_budgets(
    max_proposals: object,
    max_metadata_bytes: object,
    max_description_chars: object,
    max_asset_bytes: object,
    timeout_seconds: object,
) -> bool:
    return (
        type(max_proposals) is int
        and max_proposals >= 1
        and type(max_metadata_bytes) is int
        and max_metadata_bytes >= 1
        and type(max_description_chars) is int
        and max_description_chars >= 1
        and type(max_asset_bytes) is int
        and max_asset_bytes >= 1
        and type(timeout_seconds) in {int, float}
        and math.isfinite(float(timeout_seconds))
        and timeout_seconds > 0
    )


def _now(clock: Callable[[], float]) -> float | None:
    value = clock()
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        return None
    return float(value)


def _envelopes(
    elements: list[SourceElement],
    asset_locations: Mapping[str, ElementAssetLocation],
) -> tuple[ParsedElementEnvelope, ...] | None:
    """Project the parsed elements onto the core-owned envelope shape.

    ``asset_id`` is carried only when the caller actually resolved a location
    for it: the SDK's promise is "non-empty means there IS an image to read",
    and an element whose asset row is gone or whose file never landed would
    make that promise false.  ``asset_mime`` comes from the location too — the
    element's own metadata never carries one, and the stored row is what the
    bytes on disk actually are.
    """

    envelopes: list[ParsedElementEnvelope] = []
    for ordinal, element in enumerate(elements, start=1):
        metadata = element.metadata
        if type(metadata) is not dict:
            return None
        if any(
            type(value) is not str
            for value in (element.element_type, element.location_label, element.text)
        ):
            return None
        asset_id = _text(metadata.get("asset_id"))
        location = asset_locations.get(asset_id) if asset_id else None
        if type(location) is not ElementAssetLocation:
            asset_id, asset_mime = "", ""
        else:
            asset_mime = location.mime if type(location.mime) is str else ""
        envelopes.append(
            ParsedElementEnvelope(
                ordinal,
                element.element_type,
                element.location_label,
                element.text,
                _text(metadata.get("caption")),
                _text(metadata.get("description")),
                asset_id,
                asset_mime,
            )
        )
    return tuple(envelopes)


def _text(value: object) -> str:
    return value if type(value) is str else ""


def _applied(
    elements: list[SourceElement],
    patches: tuple[object, ...],
    max_metadata_bytes: int,
    max_description_chars: int,
) -> tuple[list[SourceElement], str]:
    """Apply every patch, or return ``(elements, reason)`` on the first
    violation.

    The byte budget is accumulated **per contribution**, matching what the
    host told each plugin its own budget was: two contributions each writing
    their own subtree are two independent budgets, not one shared one.

    Two contributions may also address the SAME element.  Each gets its own
    key under ``extensions``; their descriptions accumulate in patch order,
    joined the same way a plugin description joins the parser's own.
    """

    result = list(elements)
    byte_counts: dict[str, int] = {}
    for patch in patches:
        reason = _patch_violation(patch, len(result), max_description_chars)
        if reason:
            return elements, reason
        baseline = result[patch.ordinal - 1]
        if type(baseline.metadata) is not dict:
            return elements, REJECT_INVALID_ELEMENT
        metadata = deepcopy(baseline.metadata)
        # ``metadata`` is already a deep copy, so this subtree is this call's
        # own object and writing into it never touches the parser's mapping.
        extensions = metadata.get("extensions")
        if extensions is None:
            extensions = {}
        elif type(extensions) is not dict:
            return elements, REJECT_INVALID_ELEMENT
        if patch.contribution_id in extensions:
            return elements, REJECT_DUPLICATE_CONTRIBUTION
        # ``metadata`` on the patch is a plain JSON dict the host thawed out of
        # the plugin's own containers, but it is still a mutable object the
        # host holds a reference to — a copy is what makes the persisted value
        # independent of anything the host or a lingering worker does next.
        payload_metadata = deepcopy(patch.metadata)
        if type(payload_metadata) is not dict:
            return elements, REJECT_INVALID_PATCH
        byte_counts[patch.contribution_id] = byte_counts.get(
            patch.contribution_id, 0
        ) + persisted_element_enrichment_size(
            plugin_id=patch.plugin_id,
            plugin_version=patch.plugin_version,
            contribution_id=patch.contribution_id,
            metadata=payload_metadata,
            description=patch.description,
        )
        if byte_counts[patch.contribution_id] > max_metadata_bytes:
            return elements, REJECT_BUDGET_EXCEEDED
        extensions[patch.contribution_id] = {
            "plugin_id": patch.plugin_id,
            "plugin_version": patch.plugin_version,
            "metadata": payload_metadata,
        }
        metadata["extensions"] = extensions
        text = baseline.text
        if patch.description:
            existing = metadata.get("description")
            if existing is not None and type(existing) is not str:
                return elements, REJECT_INVALID_ELEMENT
            # Append, never replace: the parser's own description (a markdown
            # `> **图片描述**` block, say) is the author's, and a plugin's
            # reading of the image is an addition to it.  A second
            # contribution on the same element appends to the first for the
            # same reason, in patch order.
            metadata["description"] = (
                f"{existing}\n\n{patch.description}" if existing else patch.description
            )
            text = _appended_text(baseline.element_type, text, patch.description)
        result[patch.ordinal - 1] = baseline.model_copy(
            update={"metadata": metadata, "text": text}
        )
    return result, ""


def _appended_text(element_type: str, text: str, description: str) -> str:
    """Append ``description`` to an element's retrievable ``text``.

    The contract, and it is a contract rather than a formatting preference —
    it mirrors ``parsers._element``, which decides the same thing for the
    parser's own text and is the reason the two halves must agree:

    * ``code_block`` and ``table`` keep their structure, so the description is
      appended **verbatim after a newline**.  Flattening here would put a
      fenced netlist onto one line inside an element whose whole point is that
      its line structure survived parsing.
    * every other type — ``image`` above all, which is what this point
      actually enriches — is stored as a single whitespace-flattened line, so
      the appended copy is flattened to match.  A raw newline in an element
      whose siblings have none is a formatting artefact that shows up in
      search snippets.

    An empty baseline text yields the description alone rather than a leading
    separator, and a description that flattens to nothing leaves the text
    exactly as it was.
    """

    if element_type in _STRUCTURED_TEXT_TYPES:
        return f"{text}\n{description}" if text else description
    flattened = " ".join(description.split())
    if not flattened:
        return text
    return f"{text} {flattened}".strip()


def _patch_violation(
    patch: object, count: int, max_description_chars: int
) -> str:
    """``""`` when this patch may be applied, else the reason it may not.

    Split by cause rather than collapsed into one boolean: "the plugin
    addressed an element it was not shown" and "the plugin's provenance cannot
    be persisted" are different operator problems with different fixes, and a
    single code makes them indistinguishable in the event stream.
    """

    if type(patch) is not ElementEnrichmentPatch or type(patch.ordinal) is not int:
        return REJECT_INVALID_PATCH
    if not 1 <= patch.ordinal <= count:
        return REJECT_ORDINAL_OUT_OF_RANGE
    if not valid_element_enrichment_owner(
        patch.plugin_id, patch.plugin_version, patch.contribution_id
    ):
        return REJECT_INVALID_OWNER
    if (
        type(patch.description) is not str
        or len(patch.description) > max_description_chars
    ):
        return REJECT_INVALID_DESCRIPTION
    return ""


__all__ = [
    "REJECT_BUDGET_EXCEEDED",
    "REJECT_DUPLICATE_CONTRIBUTION",
    "REJECT_HOST_FAILED",
    "REJECT_INVALID_BUDGET",
    "REJECT_INVALID_DESCRIPTION",
    "REJECT_INVALID_ELEMENT",
    "REJECT_INVALID_OWNER",
    "REJECT_INVALID_PATCH",
    "REJECT_ORDINAL_OUT_OF_RANGE",
    "enrich_source_elements",
]
