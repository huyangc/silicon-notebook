"""Sanitized live projection of the loaded extension topology for admin ops.

Mirrors ``ui_projection.py`` in shape but serves a different consumer:
``GET /admin/extensions`` gives a deployment operator a read-only view of
*what actually registered*, not what a browser workspace can render.

Whitelist only, six fields per extension — ``id`` / ``version`` / ``trust`` /
``display_name`` / ``contributions`` / ``ui_contributions``. Never a module
path, a file path, a plugin's ``settings`` value, an internal availability
``reason_code``, or exception text: none of those cross this boundary.

There is deliberately no ``enabled`` field. The registry topology is
startup-frozen (see ``ExtensionRegistry.freeze``); a plugin entry the
deployment marked ``enabled = false`` in ``EXTENSIONS_CONFIG`` is never
imported and never registered, so it simply does not appear in
``registry.manifests()``. A boolean that could only ever read ``true`` for
every row this function can see would not describe anything — it would just
be a decoration an operator could misread as live health.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.extensions.registry import ExtensionRegistry


@dataclass(frozen=True)
class AdminExtensionContributionProjection:
    id: str
    point: str
    kind: str


@dataclass(frozen=True)
class AdminExtensionUiProjection:
    id: str
    slot: str
    capability: str


@dataclass(frozen=True)
class LoadedExtensionProjection:
    id: str
    version: str
    trust: str
    display_name: str
    contributions: tuple[AdminExtensionContributionProjection, ...]
    ui_contributions: tuple[AdminExtensionUiProjection, ...]


def project_loaded_extensions(
    registry: ExtensionRegistry,
) -> tuple[LoadedExtensionProjection, ...]:
    """Sanitized, static view of the frozen registry topology.

    Unlike ``project_ui_contributions`` this never evaluates a capability
    decision — there is no live per-request state here, only the manifest
    metadata every bundle declared at startup — so it is safe to call with
    no request context at all.
    """

    rows = [
        LoadedExtensionProjection(
            id=manifest.id,
            version=manifest.version,
            trust=manifest.trust,
            display_name=manifest.display_name,
            contributions=tuple(
                sorted(
                    (
                        AdminExtensionContributionProjection(
                            id=declaration.id,
                            point=declaration.point,
                            kind=declaration.kind.value,
                        )
                        for declaration in manifest.contributions
                    ),
                    key=lambda item: item.id,
                )
            ),
            ui_contributions=tuple(
                sorted(
                    (
                        AdminExtensionUiProjection(
                            id=declaration.id,
                            slot=declaration.slot,
                            capability=declaration.capability,
                        )
                        for declaration in manifest.ui_contributions
                    ),
                    key=lambda item: item.id,
                )
            ),
        )
        for manifest in registry.manifests()
    ]
    return tuple(sorted(rows, key=lambda item: item.id))
