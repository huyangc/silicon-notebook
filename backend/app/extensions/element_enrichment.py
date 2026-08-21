"""Governed runner for ``source.element_enricher`` contributions."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from app.domain.extensions import (
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ParsedElementEnvelope,
)
from app.extension_sdk import (
    SOURCE_ELEMENT_ENRICHER_POINT,
    AvailabilityStatus,
    ContributionKind,
    ContributorResult,
    ElementEnrichmentAvailabilityContext,
    ElementEnrichmentBudget,
    ElementEnrichmentCandidate,
    ElementEnrichmentContext,
    ElementRef,
    ElementView,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class _Frozen:
    plugin_id: str
    plugin_version: str
    contribution_id: str
    implementation: object


class _MalformedCancellation(RuntimeError):
    pass


def _cancelled(token: object) -> bool:
    check = getattr(token, "is_set", None)
    if not callable(check):
        raise _MalformedCancellation
    try:
        value = check()
    except Exception as exc:
        raise _MalformedCancellation from exc
    if type(value) is not bool:
        raise _MalformedCancellation
    return value


def _raise_if_cancelled(token: object) -> None:
    if not _cancelled(token):
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if not callable(raiser):
        raise _MalformedCancellation
    raiser()
    raise _MalformedCancellation


def _freeze_json(value: object, *, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("metadata too deep")
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("metadata float must be finite")
        return value
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not _METADATA_KEY.fullmatch(key):
                raise ValueError("invalid metadata key")
            frozen[key] = _freeze_json(item, depth=depth + 1)
        return MappingProxyType(frozen)
    raise ValueError("metadata must be strict JSON")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


class SourceElementEnricherHost:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        frozen: list[_Frozen] = []
        for registered in registry.contributions(SOURCE_ELEMENT_ENRICHER_POINT):
            implementation = registered.contribution.implementation
            declaration = registered.contribution.declaration
            manifest = manifests[registered.plugin_id]
            if (
                declaration.kind is not ContributionKind.CONTRIBUTOR
                or type(registered.plugin_id) is not str
                or type(manifest.version) is not str
                or not callable(getattr(implementation, "enrich", None))
            ):
                raise ExtensionRegistryError("invalid source element enricher")
            frozen.append(
                _Frozen(
                    registered.plugin_id,
                    manifest.version,
                    registered.contribution.declaration.id,
                    implementation,
                )
            )
        self._registry = registry
        self._registrations = tuple(frozen)
        self._event_sink = event_sink
        self._clock = clock

    @property
    def has_contributors(self) -> bool:
        return bool(self._registrations)

    def enrich_application(
        self,
        call_context: ElementEnrichmentCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[ElementEnrichmentPatch, ...]:
        if not self._registrations:
            return ()
        if (
            type(call_context) is not ElementEnrichmentCallContext
            or type(call_context.elements) is not tuple
            or not call_context.elements
        ):
            return ()
        try:
            return self._run(
                call_context,
                event_sink if event_sink is not None else self._event_sink,
            )
        except _MalformedCancellation:
            return ()

    def _run(
        self,
        call: ElementEnrichmentCallContext,
        sink: Callable[[dict[str, object]], None] | None,
    ) -> tuple[ElementEnrichmentPatch, ...]:
        if (
            type(call) is not ElementEnrichmentCallContext
            or type(call.elements) is not tuple
            or type(call.max_proposals) is not int
            or call.max_proposals < 1
            or type(call.max_metadata_bytes) is not int
            or call.max_metadata_bytes < 1
            or type(call.max_caption_chars) is not int
            or call.max_caption_chars < 1
            or type(call.deadline_monotonic) is not float
            or not math.isfinite(call.deadline_monotonic)
        ):
            return ()
        if not self._connection_clear(call.connection_probe):
            return ()
        _raise_if_cancelled(call.cancellation)
        refs: list[ElementRef] = []
        views: list[ElementView] = []
        for expected, item in enumerate(call.elements, start=1):
            if (
                type(item) is not ParsedElementEnvelope
                or type(item.ordinal) is not int
                or item.ordinal != expected
                or any(
                    type(value) is not str
                    for value in (
                        item.element_type,
                        item.location_label,
                        item.text,
                        item.caption,
                    )
                )
            ):
                return ()
            ref = ElementRef(object())
            refs.append(ref)
            views.append(
                ElementView(
                    ref,
                    item.element_type,
                    item.location_label,
                    item.text,
                    item.caption,
                )
            )
        budget = ElementEnrichmentBudget(
            call.max_proposals,
            call.max_metadata_bytes,
            call.max_caption_chars,
            call.deadline_monotonic,
        )
        context = ElementEnrichmentContext(
            tuple(views), call.cancellation, budget
        )
        accepted: list[ElementEnrichmentPatch] = []
        remaining = call.max_proposals
        remaining_bytes = call.max_metadata_bytes
        for registration in self._registrations:
            if not self._connection_clear(call.connection_probe):
                return ()
            if self._expired(call.deadline_monotonic):
                break
            try:
                availability = self._registry.availability(
                    registration.contribution_id,
                    ElementEnrichmentAvailabilityContext(
                        registration.contribution_id, len(views), budget
                    ),
                )
            except Exception:
                availability = None
            if not self._connection_clear(call.connection_probe):
                return ()
            if self._expired(call.deadline_monotonic):
                break
            if (
                availability is None
                or type(availability.status) is not AvailabilityStatus
                or availability.status is not AvailabilityStatus.AVAILABLE
            ):
                continue
            _raise_if_cancelled(call.cancellation)
            if not self._connection_clear(call.connection_probe):
                return ()
            started = self._safe_clock()
            try:
                result = registration.implementation.enrich(context)
            except Exception:
                self._emit(sink, registration, "failed", 0, started)
                if not self._connection_clear(call.connection_probe):
                    return ()
                continue
            _raise_if_cancelled(call.cancellation)
            if not self._connection_clear(call.connection_probe):
                self._emit(sink, registration, "invalid", 0, started)
                return ()
            validated = self._validate_result(
                result,
                refs,
                registration,
                min(remaining, call.max_proposals),
                remaining_bytes,
                call.max_caption_chars,
            )
            if validated is None:
                self._emit(sink, registration, "invalid", 0, started)
                continue
            patches, used_bytes = validated
            accepted.extend(patches)
            remaining -= len(patches)
            remaining_bytes -= used_bytes
            event_status = (
                "available"
                if result.status is ExtensionResultStatus.AVAILABLE
                else "unavailable"
            )
            self._emit(sink, registration, event_status, len(patches), started)
            if remaining <= 0:
                break
        if not self._connection_clear(call.connection_probe):
            return ()
        _raise_if_cancelled(call.cancellation)
        return tuple(accepted)

    def _validate_result(
        self,
        result: object,
        refs: list[ElementRef],
        registration: _Frozen,
        remaining: int,
        max_bytes: int,
        max_caption: int,
    ) -> tuple[list[ElementEnrichmentPatch], int] | None:
        if type(result) is not ContributorResult or type(result.items) is not tuple:
            return None
        if type(result.status) is not ExtensionResultStatus:
            return None
        if result.failure is not None and type(result.failure) is not ExtensionFailure:
            return None
        if result.failure is not None and (
            type(result.failure.kind) is not ExtensionFailureKind
            or type(result.failure.code) is not str
            or not _STABLE_CODE.fullmatch(result.failure.code)
        ):
            return None
        if result.status is ExtensionResultStatus.AVAILABLE:
            if result.failure is not None:
                return None
        elif result.items or result.failure is None:
            return None
        if len(result.items) > remaining:
            return None
        by_identity = {id(ref): index for index, ref in enumerate(refs, start=1)}
        seen: set[int] = set()
        patches: list[ElementEnrichmentPatch] = []
        byte_count = 0
        for item in result.items:
            if type(item) is not ElementEnrichmentCandidate:
                return None
            ordinal = by_identity.get(id(item.element))
            if ordinal is None or refs[ordinal - 1] is not item.element or ordinal in seen:
                return None
            if type(item.caption) is not str or len(item.caption) > max_caption:
                return None
            if type(item.metadata) is not dict:
                return None
            try:
                metadata = _freeze_json(item.metadata)
                thawed = _thaw_json(metadata)
                byte_count += len(
                    json.dumps(
                        {"metadata": thawed, "caption": item.caption},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except Exception:
                return None
            if byte_count > max_bytes:
                return None
            seen.add(ordinal)
            patches.append(
                ElementEnrichmentPatch(
                    ordinal,
                    registration.plugin_id,
                    registration.plugin_version,
                    registration.contribution_id,
                    metadata,
                    item.caption,
                )
            )
        return patches, byte_count

    @staticmethod
    def _connection_clear(probe: object) -> bool:
        check = getattr(probe, "is_connection_held", None)
        if not callable(check):
            return False
        try:
            held = check()
        except Exception:
            return False
        return type(held) is bool and held is False

    def _expired(self, deadline: float) -> bool:
        now = self._safe_clock()
        return now is None or now >= deadline

    def _safe_clock(self) -> float | None:
        try:
            value = self._clock()
        except Exception:
            return None
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            return None
        return float(value)

    def _emit(
        self,
        sink: Callable[[dict[str, object]], None] | None,
        registration: _Frozen,
        status: str,
        count: int,
        started: float | None,
    ) -> None:
        if sink is None:
            return
        elapsed = 0
        finished = self._safe_clock()
        if started is not None and finished is not None:
            delta = max(0.0, finished - started) * 1000
            if math.isfinite(delta):
                elapsed = int(delta)
        try:
            sink(
                {
                    "kind": "extension_contribution",
                    "point": SOURCE_ELEMENT_ENRICHER_POINT,
                    "plugin_id": registration.plugin_id,
                    "contribution_id": registration.contribution_id,
                    "status": status,
                    "count": count,
                    "duration_ms": elapsed,
                }
            )
        except Exception:
            pass
