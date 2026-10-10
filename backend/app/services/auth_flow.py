"""Core-owned OAuth transaction orchestration; no supplier protocol code."""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit

from app.core.config import Settings
from app.domain.auth_provider import AuthProviderError, AuthProviderHostPort

AUTH_BROWSER_PROOF_BYTES = 32
AUTH_CALLBACK_PATH = "/api/auth/sso/callback"
AUTH_FRONTEND_CALLBACK_PATH = "/auth/sso/callback"
AUTH_DEFAULT_PROVIDER_LABEL = "统一认证"


def check_unified_auth_settings(settings: Settings) -> None:
    """The deployment settings unified authentication needs, checked without
    a provider: at startup for an enabled provider, and by the administrator
    switch before it turns a provider plugin on."""
    if settings.auth_optional:
        raise AuthProviderError("anonymous_auth_forbidden")
    if not settings.auth_public_base_url:
        raise AuthProviderError("callback_not_configured")
    frontend = settings.auth_frontend_base_url or settings.auth_public_base_url
    # Browser proof uses a host-only SameSite cookie. A public reverse proxy
    # keeps the browser and callback on the same host, including local ports.
    # Same scheme too: the cookie is Secure exactly when the public origin is https.
    public = urlsplit(settings.auth_public_base_url)
    if (urlsplit(frontend).hostname, urlsplit(frontend).scheme) != (public.hostname, public.scheme):
        raise AuthProviderError("callback_origin_mismatch")
    if settings.environment.lower() in {"prod", "production"} and not (
        settings.auth_allow_insecure_http
    ) and (urlsplit(frontend).scheme != "https" or public.scheme != "https"):
        raise AuthProviderError("https_required")


class AuthFlowService:
    """Per-request orchestration. It never attaches the provider to the store:
    the composition root does that once (``create_application_repository``),
    so building a flow never mutates shared state."""

    def __init__(self, store, host: AuthProviderHostPort, settings: Settings):
        self.store = store
        self.host = host
        self.settings = settings

    def validate_configuration(self):
        """The enabled provider's descriptor, or None while local login applies.

        An enabled provider with an unusable callback configuration raises
        instead of falling back to local passwords.
        """
        descriptor = self.host.describe()
        if descriptor is None:
            return None
        check_unified_auth_settings(self.settings)
        self.host.ensure_available()
        return descriptor

    def capabilities(self):
        descriptor = self.validate_configuration()
        sso = descriptor is not None
        return {
            "sso_login": sso,
            "local_login": not sso,
            "local_registration": not sso,
            "provider_label": descriptor.public_label if sso else AUTH_DEFAULT_PROVIDER_LABEL,
        }

    @property
    def callback_url(self):
        return self.settings.auth_public_base_url + AUTH_CALLBACK_PATH

    @property
    def cookie_name(self):
        return "__Host-sn-auth-browser" if self.secure_cookie else "sn-auth-browser-dev"

    @property
    def secure_cookie(self):
        return self.settings.auth_public_base_url.startswith("https://")

    def frontend_redirect(self, *, code="", error=""):
        base = self.settings.auth_frontend_base_url or self.settings.auth_public_base_url
        if not base:
            raise AuthProviderError("callback_not_configured")
        return base + AUTH_FRONTEND_CALLBACK_PATH + "?" + urlencode(
            {"code": code} if code else {"error": error}
        )

    def _require_enabled(self):
        descriptor = self.validate_configuration()
        if descriptor is None:
            raise AuthProviderError("sso_disabled")
        return descriptor

    def start(self, browser_proof):
        descriptor = self._require_enabled()
        verifier = secrets.token_urlsafe(AUTH_BROWSER_PROOF_BYTES) if descriptor.supports_pkce else ""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() if verifier else None
        state = self.store.begin(
            browser_proof, ttl_seconds=self.settings.auth_transaction_ttl_seconds,
            pkce_verifier=verifier,
        )
        return self.host.authorization_url(
            state=state, redirect_uri=self.callback_url, code_challenge=challenge,
        )

    def callback(self, state, code, browser_proof):
        self.validate_configuration()
        transaction = self.store.claim(state, browser_proof)
        # claim commits before any network I/O; provider failures cannot replay it.
        identity = self.host.authenticate(
            code=code, redirect_uri=self.callback_url,
            code_verifier=transaction.get("pkce_verifier") or None,
            timeout_seconds=self.settings.auth_provider_timeout_seconds,
        )
        self.validate_configuration()
        return self.store.stage_identity(
            transaction, identity, browser_proof,
            ttl_seconds=self.settings.auth_transaction_ttl_seconds,
        )

    def complete(self, code, browser_proof):
        self._require_enabled()
        return self.store.complete(
            code, browser_proof, session_seconds=self.settings.auth_sso_session_seconds,
        )

    def link(self, pending_id, browser_proof, login_name, password):
        self._require_enabled()
        return self.store.link(
            pending_id, browser_proof, login_name, password,
            session_seconds=self.settings.auth_sso_session_seconds,
        )

    def create(self, pending_id, browser_proof):
        self._require_enabled()
        return self.store.create(
            pending_id, browser_proof, session_seconds=self.settings.auth_sso_session_seconds,
        )
