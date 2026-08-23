"""Collect the frozen ``http.plugin_router`` contributions into mount specs.

This is the *composition* half of the plugin route seam; the *mounting* half
lives in ``app.api.extension_routes``.  They are deliberately two modules in
two packages because the dependency edges only run one way each:
``app.extensions.*`` must never import ``app.api.*`` (that would close an SCC),
and ``app.api.*`` must never import ``app.extensions.*``/``app.extension_sdk.*``
(architecture guard).  ``app.domain.extension_http.PluginRouterSpec`` is the
only shape both sides may name.

Nothing here calls a plugin's factory or touches FastAPI.  A spec is inert
data: plugin id, contribution id, the declared factory object, and that
plugin's already-validated settings instance.

Every rejection raises :class:`~app.extensions.discovery.ExtensionDiscoveryError`
— the same exception discovery itself raises — because the consequence is the
same: ``create_app()`` re-raises and the process refuses to start.  A plugin
that declares a router it is not allowed to mount must never degrade into a
silently route-less plugin.
"""
from __future__ import annotations

from collections.abc import Mapping

from app.domain.extension_http import PluginRouterSpec
from app.extension_sdk import ContributionKind
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT
from app.extensions.discovery import ExtensionDiscoveryError
from app.extensions.registry import ExtensionRegistry


def collect_plugin_router_specs(
    registry: ExtensionRegistry,
    plugin_settings: Mapping[str, object],
) -> tuple[PluginRouterSpec, ...]:
    """Freeze every legal plugin router contribution, in plugin-id order.

    Rejections, in the order checked:

    ``plugin_router_kind_invalid``
        The declaration is not a ``CONTRIBUTOR``.  The registry's typed
        ``add_*`` helpers already enforce kind-vs-declaration agreement, so
        this catches a bundle that registered through the untyped ``add``.
    ``plugin_router_trust_denied``
        A ``trust == "builtin"`` bundle contributed a router.  Core endpoints
        must stay in ``app/api/*_routes.py`` where the frozen ``api_contract``
        fixture governs them; a builtin that mounted routes through this seam
        would move part of the core API surface out from under that gate.
    ``plugin_router_multiple``
        One plugin declared a second router.  Both would mount under the same
        ``/api/extensions/{plugin_id}`` prefix, so path collisions between them
        would resolve by registration order — an invisible, order-dependent
        shadowing.  One prefix, one router.
    ``plugin_router_factory_invalid``
        The registered implementation is not callable, so it cannot be a
        router factory at all.

    Settings lookup uses ``in``, never truthiness: a plugin that declares no
    ``settings_model`` is present in the mapping with a ``None`` value, and
    ``plugin_settings.get(id)`` would be indistinguishable from a plugin that
    is missing entirely.
    """

    registered = registry.contributions(PLUGIN_HTTP_ROUTER_POINT)
    if not registered:
        # Zero deployment plugins is the shipped default, and it must cost
        # nothing: no manifest map, no sort, no allocation beyond this tuple.
        return ()

    manifests = {manifest.id: manifest for manifest in registry.manifests()}
    specs: list[PluginRouterSpec] = []
    claimed: set[str] = set()
    for record in registered:
        plugin_id = record.plugin_id
        contribution = record.contribution
        if contribution.declaration.kind is not ContributionKind.CONTRIBUTOR:
            raise ExtensionDiscoveryError(plugin_id, "plugin_router_kind_invalid")
        manifest = manifests.get(plugin_id)
        if manifest is None or manifest.trust != "deployment":
            raise ExtensionDiscoveryError(plugin_id, "plugin_router_trust_denied")
        if plugin_id in claimed:
            raise ExtensionDiscoveryError(plugin_id, "plugin_router_multiple")
        if not callable(contribution.implementation):
            raise ExtensionDiscoveryError(plugin_id, "plugin_router_factory_invalid")
        claimed.add(plugin_id)
        specs.append(
            PluginRouterSpec(
                plugin_id=plugin_id,
                contribution_id=contribution.declaration.id,
                factory=contribution.implementation,
                settings=(
                    plugin_settings[plugin_id]
                    if plugin_id in plugin_settings
                    else None
                ),
            )
        )
    # Mount order is by plugin id rather than registration order: the prefixes
    # are disjoint so this cannot change routing, but it makes the mounted
    # topology a function of the config's contents instead of its ordering.
    return tuple(sorted(specs, key=lambda spec: spec.plugin_id))
