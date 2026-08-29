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


# Sort key shared by `ui_contribution_contract` (below) and
# `scripts/generate_ui_extension_contract.py` (which re-sorts the already-
# sorted output purely to stay idempotent — see that script's docstring).
# Field order matches the wire shape so a stable tie-break is total even
# when two plugins declare contributions with colliding ids across slots.
CONTRIBUTION_SORT_FIELDS = (
    "plugin_id", "version", "contribution_id", "slot", "capability",
)


def _contribution_sort_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(field, "") for field in CONTRIBUTION_SORT_FIELDS)


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

    Rows are returned sorted by `CONTRIBUTION_SORT_FIELDS` (not registry
    order) so every caller — the live `/api/system/extensions` equivalent
    static contract, the committed fixture generator, and the pytest parity
    assertion that compares this function's output against that fixture
    byte-for-byte — sees the same deterministic order without each having to
    re-sort it independently.
    """

    rows = [{
        "plugin_id": manifest.id,
        "version": manifest.version,
        "contribution_id": declaration.id,
        "slot": declaration.slot,
        "capability": declaration.capability,
    } for manifest, declaration in registry.ui_contributions()]
    return sorted(rows, key=_contribution_sort_key)


def project_ui_contributions(
    registry: ExtensionRegistry,
    context: object | None,
) -> tuple[UiContributionProjection, ...]:
    """Evaluate each declared capability live and expose only a closed wire shape.

    A row whose plugin an administrator switched off is settled before its
    capability is looked at: the workspace must stop offering the entry the
    moment the toggle flips, whatever the plugin's own decision would have
    said, and asking a disabled plugin's decision for an answer nobody would
    use is work this projection has no reason to do. Only a deployment plugin
    can be in that state — ``plugin_runtime_disabled`` is False for every
    built-in — so built-in rows keep taking the path below byte for byte.
    ``"disabled"`` is the reason the wire already carries for
    ``AvailabilityStatus.DISABLED``; the gate adds no new public value, which
    is why the gated verdict is the loop's *initial* one and every row is then
    built from one expression.
    """

    rows: list[UiContributionProjection] = []
    statuses: dict[str, AvailabilityStatus] = {}
    for manifest, declaration in registry.ui_contributions():
        available = False
        unavailable_reason: PublicUiUnavailableReason | None = "disabled"
        if not registry.plugin_runtime_disabled(manifest.id):
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
            unavailable_reason = None
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
