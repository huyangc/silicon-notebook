"""Core admission adapter for parsed-element metadata contributions."""
from __future__ import annotations

import math
import re
import time
from copy import deepcopy
from types import MappingProxyType
from typing import Callable

from app.domain.extensions import (
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ElementEnricherHostPort,
    ParsedElementEnvelope,
)
from app.domain.element_enrichment import (
    persisted_element_enrichment_size,
    valid_element_enrichment_owner,
)
from app.models.sources import SourceElement


class _NeverCancelled:
    __slots__ = ()

    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


def enrich_source_elements(
    elements: list[SourceElement],
    *,
    host: ElementEnricherHostPort | None,
    connection_probe: object,
    max_proposals: int,
    max_metadata_bytes: int,
    max_caption_chars: int,
    timeout_seconds: float,
    event_sink: Callable[[dict[str, object]], None] | None,
    clock: Callable[[], float] = time.monotonic,
) -> list[SourceElement]:
    if host is None or not elements:
        return elements
    if (
        type(max_proposals) is not int
        or max_proposals < 1
        or type(max_metadata_bytes) is not int
        or max_metadata_bytes < 1
        or type(max_caption_chars) is not int
        or max_caption_chars < 1
        or type(timeout_seconds) not in {int, float}
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        return elements
    try:
        if host.has_contributors is not True:
            return elements
    except Exception:
        return elements
    try:
        now = clock()
    except Exception:
        return elements
    if type(now) not in {int, float} or not math.isfinite(float(now)):
        return elements
    try:
        envelopes_list: list[ParsedElementEnvelope] = []
        for index, element in enumerate(elements, start=1):
            metadata = element.metadata
            if type(metadata) is not dict:
                return elements
            caption = metadata.get("caption", "")
            if type(caption) is not str:
                caption = ""
            fields = (
                element.element_type,
                element.location_label,
                element.text,
                caption,
            )
            if any(type(field) is not str for field in fields):
                return elements
            envelopes_list.append(
                ParsedElementEnvelope(index, fields[0], fields[1], fields[2], fields[3])
            )
        envelopes = tuple(envelopes_list)
        patches = host.enrich_application(
            ElementEnrichmentCallContext(
                envelopes,
                _NeverCancelled(),
                connection_probe,
                max_proposals,
                max_metadata_bytes,
                max_caption_chars,
                float(now) + timeout_seconds,
            ),
            event_sink=event_sink,
        )
    except Exception:
        return elements
    if type(patches) is not tuple:
        return elements
    if not patches:
        return elements
    if len(patches) > max_proposals:
        return elements
    try:
        result = list(elements)
        byte_count = 0
        provenance: dict[str, tuple[str, str]] = {}
        for patch in patches:
            if (
                type(patch) is not ElementEnrichmentPatch
                or type(patch.ordinal) is not int
                or not 1 <= patch.ordinal <= len(result)
                or not valid_element_enrichment_owner(
                    patch.plugin_id,
                    patch.plugin_version,
                    patch.contribution_id,
                )
                or type(patch.caption) is not str
                or len(patch.caption) > max_caption_chars
            ):
                return elements
            baseline = result[patch.ordinal - 1]
            if type(baseline.metadata) is not dict:
                return elements
            metadata = deepcopy(baseline.metadata)
            existing_extensions = metadata.get("extensions")
            if existing_extensions is None:
                extensions: dict[str, object] = {}
            elif type(existing_extensions) is dict:
                extensions = deepcopy(existing_extensions)
            else:
                return elements
            if patch.contribution_id in extensions:
                return elements
            patch_metadata = _thaw(patch.metadata)
            byte_count += persisted_element_enrichment_size(
                plugin_id=patch.plugin_id,
                plugin_version=patch.plugin_version,
                contribution_id=patch.contribution_id,
                metadata=patch_metadata,
                caption=patch.caption,
            )
            if byte_count > max_metadata_bytes:
                return elements
            owner = (patch.plugin_id, patch.plugin_version)
            previous_owner = provenance.setdefault(patch.contribution_id, owner)
            if previous_owner != owner:
                return elements
            payload = {
                "plugin_id": patch.plugin_id,
                "plugin_version": patch.plugin_version,
                "metadata": patch_metadata,
            }
            if patch.caption:
                if metadata.get("caption") or patch.caption not in baseline.text:
                    return elements
                payload["caption"] = patch.caption
            extensions[patch.contribution_id] = payload
            metadata["extensions"] = extensions
            result[patch.ordinal - 1] = baseline.model_copy(
                update={"metadata": metadata}
            )
        return result
    except Exception:
        return elements


def _thaw(value: object, *, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("extension metadata too deep")
    if type(value) is MappingProxyType or type(value) is dict:
        thawed: dict[str, object] = {}
        for key, item in value.items():  # type: ignore[union-attr]
            if type(key) is not str or _METADATA_KEY.fullmatch(key) is None:
                raise TypeError("invalid extension metadata key")
            thawed[key] = _thaw(item, depth=depth + 1)
        return thawed
    if type(value) is tuple:
        return [_thaw(item, depth=depth + 1) for item in value]
    if type(value) is float and not math.isfinite(value):
        raise ValueError("invalid extension metadata float")
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise TypeError("invalid extension metadata")


_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
