"""Sanitized live projection of metadata-only workspace UI contributions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.extension_sdk import AvailabilityStatus
from app.extensions.registry import ExtensionRegistry


PublicUiUnavailableReason = Literal["disabled", "unavailable"]


@dataclass(frozen=True)
class UiContributionProjection:
    plugin_id: str
    display_name: str
    version: str
    contribution_id: str
    available: bool
    unavailable_reason: PublicUiUnavailableReason | None


def ui_contribution_contract(registry: ExtensionRegistry) -> list[dict[str, str]]:
    """Static cross-stack UI topology contract (no live availability evaluation).

    This is the shape compared against the frontend's build-time UI registry
    (`frontend/tests/guards/extension-ui-parity.test.mjs`) and against the
    committed fixture `backend/tests/fixtures/ui_extension_contract.json`.
    Unlike `project_ui_contributions`, it never evaluates a capability
    decision or touches a request `context`, so it is safe to call from a
    test, at import time, or from an offline generator script — it is the
    single source both consume; do not re-spell this list comprehension a
    second time.
    """

    return [{
        "plugin_id": manifest.id,
        "version": manifest.version,
        "contribution_id": declaration.id,
        "slot": declaration.slot,
        "capability": declaration.capability,
    } for manifest, declaration in registry.ui_contributions()]


def project_ui_contributions(
    registry: ExtensionRegistry,
    context: object | None,
) -> tuple[UiContributionProjection, ...]:
    """Evaluate each declared capability live and expose only a closed wire shape."""

    rows: list[UiContributionProjection] = []
    statuses: dict[str, AvailabilityStatus] = {}
    for manifest, declaration in registry.ui_contributions():
        status = statuses.get(declaration.capability)
        if status is None:
            try:
                availability = registry.capability_availability(
                    declaration.capability,
                    context,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                status = AvailabilityStatus.UNAVAILABLE
            else:
                status = availability.status
            statuses[declaration.capability] = status
        available = status is AvailabilityStatus.AVAILABLE
        unavailable_reason: PublicUiUnavailableReason | None = None
        if not available:
            unavailable_reason = (
                "disabled"
                if status is AvailabilityStatus.DISABLED
                else "unavailable"
            )
        rows.append(UiContributionProjection(
            plugin_id=manifest.id,
            display_name=manifest.display_name,
            version=manifest.version,
            contribution_id=declaration.id,
            available=available,
            unavailable_reason=unavailable_reason,
        ))
    return tuple(rows)
