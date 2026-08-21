"""Startup-frozen capability names with request-time availability decisions."""
from __future__ import annotations

from types import MappingProxyType
import re
from typing import Callable, Mapping

from app.extension_sdk import Availability, AvailabilityStatus


CapabilityDecision = Callable[[object | None], Availability]
_STABLE_REASON = re.compile(r"^[a-z][a-z0-9_]*$")


class CapabilityDecisionCatalog:
    """Immutable decision-entry catalog; each decision remains live per call."""

    def __init__(self, entries: Mapping[str, CapabilityDecision] | None = None) -> None:
        source = dict(entries or {})
        if any(not name or not callable(decision) for name, decision in source.items()):
            raise ValueError("capability decision entries must be named callables")
        self._entries = MappingProxyType(source)

    def has(self, capability: str) -> bool:
        return capability in self._entries

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def availability(
        self, capability: str, context: object | None = None
    ) -> Availability:
        decision = self._entries.get(capability)
        if decision is None:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="unknown_capability",
            )
        try:
            result = decision(context)
        except Exception:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="capability_decision_failed",
            )
        if type(result) is not Availability:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_capability_decision",
            )
        if not isinstance(result.status, AvailabilityStatus):
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_capability_status",
            )
        if type(result.reason_code) is not str:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_capability_reason",
            )
        reason = result.reason_code
        if reason and not _STABLE_REASON.fullmatch(reason):
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_capability_reason",
            )
        return result


EMPTY_CAPABILITY_CATALOG = CapabilityDecisionCatalog()
