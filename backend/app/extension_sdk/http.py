"""Dependency-light contracts for the plugin HTTP router extension point.

A ``trust == "deployment"`` bundle may contribute at most one router here via
``ContributionDeclaration(point=PLUGIN_HTTP_ROUTER_POINT, kind=CONTRIBUTOR)``.
Its factory receives only a ``PluginRouteContext`` — never the repository,
global Settings, a model client, the FastMCP host, or a raw bearer token —
and returns a router that core mounts under
``{PLUGIN_ROUTE_PREFIX}/{plugin_id}`` behind router-level session
authentication. See ``app.domain.extension_http`` for the full contract; this
module only re-exports it for plugin code plus the point name.
"""
from __future__ import annotations

from app.domain.extension_http import (
    PLUGIN_ROUTE_PREFIX,
    PluginActor,
    PluginImportedSource,
    PluginRejectedUrl,
    PluginRouteContext,
    PluginRouterFactory,
    PluginRouterSpec,
    PluginUrlImportResult,
    PluginUrlSourceImportPort,
)


# Contribution point name a deployment plugin declares its router
# contribution against. At most one per plugin (see
# app.extensions.http_router.collect_plugin_router_specs).
PLUGIN_HTTP_ROUTER_POINT = "http.plugin_router"


__all__ = [
    "PLUGIN_HTTP_ROUTER_POINT",
    "PLUGIN_ROUTE_PREFIX",
    "PluginActor",
    "PluginImportedSource",
    "PluginRejectedUrl",
    "PluginRouteContext",
    "PluginRouterFactory",
    "PluginRouterSpec",
    "PluginUrlImportResult",
    "PluginUrlSourceImportPort",
]
