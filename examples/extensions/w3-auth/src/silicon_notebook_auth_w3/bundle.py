"""Deployment bundle exposed to ``EXTENSIONS_CONFIG`` as ``BUNDLE``."""
from __future__ import annotations

from dataclasses import dataclass, field
import os

from app.extension_sdk import (
    AUTH_PROVIDER_POINT,
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)

from .provider import W3AuthProvider
from .settings import W3AuthSettings


PLUGIN_ID = "examples.w3_auth"
_PROVIDER = ContributionDeclaration(
    id=f"{PLUGIN_ID}.provider",
    point=AUTH_PROVIDER_POINT,
    kind=ContributionKind.PROVIDER,
)


@dataclass
class W3AuthBundle:
    manifest: ExtensionManifest
    settings_model: type[W3AuthSettings] = W3AuthSettings
    settings: W3AuthSettings | None = None
    provider: W3AuthProvider = field(init=False)

    def __post_init__(self) -> None:
        self.provider = W3AuthProvider(lambda: self.settings)

    def configure(self, settings: W3AuthSettings) -> None:
        self.settings = settings

    def register(self, registrar) -> None:
        registrar.add_provider(ExtensionContribution(
            declaration=_PROVIDER,
            implementation=self.provider,
            availability=self._available,
        ))

    def _available(self, _context: object | None) -> Availability:
        settings = self.settings
        if settings is None:
            return Availability(AvailabilityStatus.DISABLED, "not_configured")
        required = (
            settings.login_origin_env,
            settings.client_id_env,
            settings.client_secret_env,
        )
        if any(not os.environ.get(name, "").strip() for name in required):
            return Availability(AvailabilityStatus.DISABLED, "credentials_missing")
        return Availability.available()


BUNDLE = W3AuthBundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="W3 统一认证示例",
    trust="deployment",
    contributions=(_PROVIDER,),
    requires=(),
    provides=(),
    ui_contributions=(),
))


__all__ = ["BUNDLE", "PLUGIN_ID", "W3AuthBundle"]
