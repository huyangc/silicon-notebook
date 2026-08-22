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
