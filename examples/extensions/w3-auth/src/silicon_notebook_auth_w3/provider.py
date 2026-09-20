"""W3 implementation of the provider-neutral authentication SDK."""
from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Callable

import httpx

from app.extension_sdk import (
    AuthProviderCodeContext,
    AuthProviderDescription,
    ExternalIdentity,
)

from .client import W3Client, login_origin, required_environment
from .settings import W3AuthSettings


_AUTHORIZE_PATH = "/saaslogin1/oauth2/authorize"


class W3AuthProvider:
    def __init__(
        self,
        settings_source: Callable[[], W3AuthSettings | None],
        *,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings_source = settings_source
        self._transport = transport
        self._environ = os.environ if environ is None else environ

    def describe(self) -> AuthProviderDescription:
        settings = self._settings()
        origin = login_origin(settings, self._environ)
        return AuthProviderDescription(
            provider_id=settings.provider_id,
            provider_namespace=settings.provider_namespace,
            configuration_generation=settings.configuration_generation,
            public_label=settings.public_label,
            authorization_endpoint=f"{origin}{_AUTHORIZE_PATH}",
            supports_pkce=settings.pkce_supported,
        )

    def authorization_parameters(self) -> tuple[tuple[str, str], ...]:
        settings = self._settings()
        client_id = required_environment(settings.client_id_env, self._environ)
        return (
            ("client_id", client_id),
            ("response_type", "code"),
            ("scope", "base.profile"),
            ("display", "page"),
        )

    def authenticate_code(
        self, context: AuthProviderCodeContext
    ) -> ExternalIdentity:
        return W3Client(
            self._settings(),
            transport=self._transport,
            environ=self._environ,
        ).authenticate(context)

    def _settings(self) -> W3AuthSettings:
        settings = self._settings_source()
        if type(settings) is not W3AuthSettings:
            from app.extension_sdk import AuthProviderFailure, AuthProviderFailureKind

            raise AuthProviderFailure(AuthProviderFailureKind.CONFIGURATION)
        return settings


__all__ = ["W3AuthProvider"]
