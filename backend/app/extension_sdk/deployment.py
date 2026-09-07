"""Documentation-only contracts a ``trust == "deployment"`` bundle satisfies.

These are Protocols, not base classes — a plugin module needs to expose a
module-level object shaped like ``DeploymentExtensionBundle`` (optionally
``CapabilityProvidingBundle`` too), not import and subclass anything from
this SDK. A plugin that satisfies them by duck typing alone is fully
conformant.

These Protocols describe the widest conformant shape. The authority on what is
actually admitted is ``app.extensions.discovery``'s explicit checks at process
startup — read the per-class notes below for where the two differ.

The operator-facing statement of the ``configure`` cost rule and the
capability-naming rule lives in ``docs/deployment-extensions-sop.md`` §3.2 and
§3.3 (Chinese pair ``docs/deployment-extensions-sop_zh.md``); the docstrings
below restate them for readers arriving from the code.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.extension_sdk.contracts import (
    AvailabilityProbe,
    ExtensionManifest,
    ExtensionRegistrar,
)


class DeploymentExtensionBundle(Protocol):
    """The widest shape a deployment plugin's module-level bundle may take.

    This Protocol is documentation, not the admission gate: what a bundle must
    actually satisfy is decided by the explicit structural checks in
    ``app.extensions.discovery``, and those are narrower in one place and wider
    in another.  Narrower: ``manifest`` must be an ``ExtensionManifest`` whose
    ``id`` equals the config key, whose ``trust`` is ``"deployment"``, and whose
    ``api_version`` matches this build.  Wider: ``settings_model`` and
    ``configure`` are a *pair* that may be omitted **together** — a plugin that
    takes no configuration declares neither, and any ``[settings]`` table for it
    is then a startup failure.  Declaring exactly one of the two is rejected
    (``plugin_settings_binding_missing``).

    When both are present, ``settings_model`` is a pydantic ``BaseModel``
    subclass and ``configure`` is called with one already-validated instance of
    it — built from the TOML ``[settings]`` table — before ``register``.

    ``configure`` must be cheap and side-effect-free with respect to the
    process: store the instance and return.  It runs inside startup composition,
    before the registry is frozen and before the service is ready, so it must
    not start threads or background tasks, open network or database
    connections, or perform blocking I/O.  Do that work lazily, on the first
    request that needs it.
    """

    manifest: ExtensionManifest
    settings_model: Any

    def configure(self, settings: Any) -> None: ...

    def register(self, registrar: ExtensionRegistrar) -> None: ...


class CapabilityProvidingBundle(Protocol):
    """The additional contract a bundle satisfies when ``manifest.provides``
    is non-empty: one probe per declared capability name.

    The mapping's keys must equal ``manifest.provides`` exactly — a probe for an
    undeclared name and a declared name without a probe are both startup
    failures — and each name must be a stable metadata id that collides with
    neither a core capability nor another plugin's.
    """

    manifest: ExtensionManifest
    capability_decisions: Mapping[str, AvailabilityProbe]

    def register(self, registrar: ExtensionRegistrar) -> None: ...


__all__ = [
    "CapabilityProvidingBundle",
    "DeploymentExtensionBundle",
]
