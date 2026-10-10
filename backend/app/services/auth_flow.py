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


class AuthFlowService:
    def __init__(self, store, host: AuthProviderHostPort, settings: Settings):
        self.store = store
        self.host = host
        self.settings = settings
        # The store reads "unified auth is on" from this same host, so session
        # resolution and local-credential refusals follow the provider switch.
        store.use_provider(host)

    def validate_configuration(self):
        """The enabled provider's descriptor, or None while local login applies.

        An enabled provider with an unusable callback configuration raises
        instead of falling back to local passwords.
        """
        descriptor = self.host.describe()
        if descriptor is None:
            return None
        if self.settings.auth_optional:
            raise AuthProviderError("anonymous_auth_forbidden")
        if not self.settings.auth_public_base_url:
            raise AuthProviderError("callback_not_configured")
        frontend = self.settings.auth_frontend_base_url or self.settings.auth_public_base_url
        # Browser proof uses a host-only SameSite cookie. A public reverse proxy
        # keeps the browser and callback on the same host, including local ports.
        # Same scheme too: the cookie is Secure exactly when the public origin is https.
        public = urlsplit(self.settings.auth_public_base_url)
        if (urlsplit(frontend).hostname, urlsplit(frontend).scheme) != (public.hostname, public.scheme):
            raise AuthProviderError("callback_origin_mismatch")
        if self.settings.environment.lower() in {"prod", "production"} and not (
            self.settings.auth_allow_insecure_http
        ) and (
            urlsplit(frontend).scheme != "https"
            or urlsplit(self.settings.auth_public_base_url).scheme != "https"
        ):
            raise AuthProviderError("https_required")
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
