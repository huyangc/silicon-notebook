"""Documentation-only contracts a ``trust == "deployment"`` bundle satisfies.

These are Protocols, not base classes — a plugin module needs to expose a
module-level object shaped like ``DeploymentExtensionBundle`` (optionally
``CapabilityProvidingBundle`` too), not import and subclass anything from
this SDK. ``app.extensions.discovery`` structurally validates the loaded
object against these shapes at process startup; a plugin that satisfies them
by duck typing alone is fully conformant.
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
    """The contract every deployment plugin module-level bundle satisfies.

    ``settings_model`` is a pydantic ``BaseModel`` subclass (or ``None`` when
    the plugin accepts no ``[extensions.<id>.settings]`` table).  When it is
    not ``None``, ``configure`` is called with one already-validated instance
    of it — built from the TOML ``[settings]`` table — before ``register``.
    """

    manifest: ExtensionManifest
    settings_model: Any

    def configure(self, settings: Any) -> None: ...

    def register(self, registrar: ExtensionRegistrar) -> None: ...


class CapabilityProvidingBundle(Protocol):
    """The additional contract a bundle satisfies when ``manifest.provides``
    is non-empty: one probe per declared capability name.
    """

    manifest: ExtensionManifest
    capability_decisions: Mapping[str, AvailabilityProbe]

    def register(self, registrar: ExtensionRegistrar) -> None: ...


__all__ = [
    "CapabilityProvidingBundle",
    "DeploymentExtensionBundle",
]
