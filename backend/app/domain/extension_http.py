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
import threading
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
    # Site-level system administrator (``users.role == "admin"``), and nothing
    # more.  This is NOT notebook-scoped authorization: it says nothing about
    # whether this user may read or write any particular notebook.  Gate
    # notebook access with ``require_notebook_capability`` /
    # ``require_notebook_read`` from the route context — never by branching on
    # this flag.
    is_admin: bool


@dataclass(frozen=True, slots=True)
class PluginImportedSource:
    source_id: str
    # ``sources.title`` as written at creation, which is the URL-derived name:
    # paper-metadata grounding has not run yet at this instant, so the richer
    # ``display_title`` would be empty here regardless.  A plugin that shows a
    # source name to a user should read it back later through core's
    # ``source_display_title`` rules rather than persisting this value as the
    # display name.
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class PluginRejectedUrl:
    url: str
    # User-facing Chinese copy explaining the rejection, produced by core's
    # existing import path.  It is NOT a stable reason code: the wording may
    # change with UI copy, so a plugin must never branch on it.
    reason: str


@dataclass(frozen=True, slots=True)
class PluginUrlImportResult:
    created: tuple[PluginImportedSource, ...]
    rejected: tuple[PluginRejectedUrl, ...]


class PluginUrlSourceImportPort(Protocol):
    """The only way a plugin route may add sources to a notebook.

    **The port authorizes itself.** Core checks ``sources:write`` on
    ``notebook_id`` for the request's own authenticated user — resolved from
    core's request context, never from anything the caller passes — before any
    source is created, and refuses with the same 404 core's own endpoints use
    (existence is not disclosed). A plugin therefore cannot widen its reach by
    choosing a permissive route shape: mounting only the read gate, naming the
    path parameter something other than ``{notebook_id}``, or taking the id out
    of a request body all end at the same check.

    Past that check this reuses core's existing capacity accounting, admin
    exemptions, and unconfigured-parser mapping (see
    ``app.api.source_routes.import_url_sources``) instead of handing the plugin
    a repository or a model client.

    **Two call shapes, one implementation — which one you use is not a style
    choice.** The work behind this port blocks: database writes plus one
    serial remote probe per URL, so a single slow host holds the calling
    thread for seconds.

    * A **sync** handler (``def``) is already running in FastAPI's threadpool,
      so it calls :meth:`import_urls` directly.
    * An **async** handler (``async def``) runs *on the event loop thread*, so
      it must ``await`` :meth:`import_urls_async`, which offloads the same work
      to the threadpool. Calling :meth:`import_urls` there would block the one
      thread every other in-flight request in the process is sharing.

    Mixing the two up is refused at runtime rather than merely discouraged:
    :meth:`import_urls` raises ``RuntimeError`` when it finds a running event
    loop on its own thread, and the message names the method to await instead.
    """

    def import_urls(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult:
        """Import synchronously. Only from a **sync** (``def``) handler.

        Raises ``RuntimeError`` if called on the event loop thread — see
        :meth:`import_urls_async` for what to call from an ``async def``
        handler.
        """
        ...

    async def import_urls_async(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult:
        """Import from an **async** (``async def``) handler.

        Same authorization, same core implementation, same result — the only
        difference is that the blocking work runs in the threadpool instead of
        on the event loop thread. The request's authenticated user is carried
        into that thread with the rest of the request context, so the port
        authorizes exactly as it does on the sync path.
        """
        ...


class PluginTaskStreamPort(Protocol):
    """Core-owned request-local NDJSON transport for plugin HTTP work.

    A plugin route uses this only when the browser is waiting directly for a
    slow, synchronous operation such as an upstream model/search call. Core
    runs ``work`` off the event-loop thread, sends the shared
    ``started``/``heartbeat``/``final`` protocol, and turns every worker
    exception into a stable, content-free error frame. The event passed to
    ``work`` is set when the browser disconnects, so a cooperative upstream
    client can stop promptly.

    Durable/background work does not belong here: it must persist its own job
    state and let the browser reconnect or poll instead of tying its lifetime
    to this response.
    """

    def response(
        self,
        request: Any,
        work: Callable[[threading.Event], Any],
        *,
        stage: str,
    ) -> Any:
        """Return the shared NDJSON response for one synchronous callable."""
        ...


@dataclass(frozen=True, slots=True)
class PluginRouteContext:
    """Everything a deployment plugin's router factory is given — nothing more.

    Deliberately excludes the repository, global Settings, a model client,
    the FastMCP host, and any raw bearer token. A plugin router can only
    reach core through these nine narrow seams.
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
    current_actor: Callable[..., PluginActor]
    # Builds a user-facing 4xx exception whose detail routes through core's
    # user_error() plumbing (X-User-Message), so plugin error text follows
    # the same UI-copy rules as core endpoints.
    user_error: Callable[[int, str], Exception]
    url_sources: PluginUrlSourceImportPort
    # Request-local interactive work only. It keeps the browser connection
    # alive, runs a synchronous callable off the event loop, and propagates a
    # cooperative disconnect signal without exposing core's model client.
    task_stream: PluginTaskStreamPort
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
