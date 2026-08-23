"""Stable domain contracts for deployment-plugin HTTP routes.

These live in ``app.domain`` — not ``app.api`` and not ``app.extension_sdk`` —
because both boundaries are one-directional and neither can host this shape
on its own: ``app.api`` must never import ``app.extensions``/``app.extension_sdk``
(architecture guard), and the composition root that mounts plugin routers
(``app.bootstrap``) must never import ``app.api``. Domain is the only package
both sides may import, so the wire types for the plugin route seam are
declared here and re-exported by ``app.extension_sdk.http`` for plugin code.

No repository, model client, settings, or transport imports — only stdlib.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


# The single mount point every deployment plugin router lands under:
# ``{PLUGIN_ROUTE_PREFIX}/{plugin_id}``. Re-exported by the SDK so a plugin
# never has to hardcode it, and read by the core mount helper so there is
# exactly one spelling of the prefix.
PLUGIN_ROUTE_PREFIX = "/api/extensions"


@dataclass(frozen=True, slots=True)
class PluginActor:
    """The narrow, read-only view of the current user a plugin route gets.

    No email, no session token, no raw user row — just enough to gate a
    plugin's own business logic on identity and admin status.
    """

    id: str
    is_admin: bool


@dataclass(frozen=True, slots=True)
class PluginImportedSource:
    source_id: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class PluginRejectedUrl:
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class PluginUrlImportResult:
    created: tuple[PluginImportedSource, ...]
    rejected: tuple[PluginRejectedUrl, ...]


class PluginUrlSourceImportPort(Protocol):
    """The only way a plugin route may add sources to a notebook.

    This reuses core's existing capacity accounting, admin exemptions, and
    unconfigured-parser mapping (see ``app.api.source_routes.import_url_sources``)
    instead of handing the plugin a repository or a model client.
    """

    def import_urls(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult: ...


@dataclass(frozen=True, slots=True)
class PluginRouteContext:
    """Everything a deployment plugin's router factory is given — nothing more.

    Deliberately excludes the repository, global Settings, a model client,
    the FastMCP host, and any raw bearer token. A plugin router can only
    reach core through these eight narrow seams.
    """

    plugin_id: str
    # The plugin's own validated settings instance (its settings_model,
    # already bound), or None when the plugin declares no settings_model.
    settings: Any
    # FastAPI dependency factory for core's write-capability gates
    # (e.g. "sources:write"); call it with a capability name to get a
    # Depends(...)-able object.
    require_notebook_capability: Callable[[str], Any]
    # FastAPI dependency: core's notebook *read* gate (decision 2) — lets a
    # plugin mount a read-only notebook-scoped route without a write gate.
    require_notebook_read: Any
    # FastAPI dependency resolving to a PluginActor for the current request.
    current_actor: Callable[..., Any]
    # Builds a user-facing 4xx exception whose detail routes through core's
    # user_error() plumbing (X-User-Message), so plugin error text follows
    # the same UI-copy rules as core endpoints.
    user_error: Callable[[int, str], Exception]
    url_sources: PluginUrlSourceImportPort
    # Emits one whitelisted observability event; malformed payloads are
    # dropped by core rather than raised back into the plugin.
    emit_event: Callable[[Mapping[str, object]], None]


class PluginRouterFactory(Protocol):
    def __call__(self, context: PluginRouteContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class PluginRouterSpec:
    """One deployment plugin's frozen http.plugin_router contribution.

    Produced by ``app.extensions.http_router.collect_plugin_router_specs``
    and consumed by ``app.api.extension_routes.mount_extension_routers`` —
    the only two places a plugin's router factory is ever invoked.
    """

    plugin_id: str
    contribution_id: str
    factory: PluginRouterFactory
    settings: Any
