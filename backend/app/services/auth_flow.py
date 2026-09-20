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


class AuthFlowService:
    def __init__(self, store, host: AuthProviderHostPort, settings: Settings):
        self.store = store
        self.host = host
        self.settings = settings

    def validate_configuration(self, policy=None):
        policy = policy if policy is not None else self.store.get_policy()
        if policy["mode"] == "local":
            return None
        if self.settings.auth_optional:
            raise AuthProviderError("anonymous_auth_forbidden")
        if not self.settings.auth_public_base_url:
            raise AuthProviderError("callback_not_configured")
        frontend = self.settings.auth_frontend_base_url or self.settings.auth_public_base_url
        # Browser proof uses a host-only SameSite cookie. A public reverse proxy
        # keeps the browser and callback on the same host, including local ports.
        if urlsplit(frontend).hostname != urlsplit(self.settings.auth_public_base_url).hostname:
            raise AuthProviderError("callback_origin_mismatch")
        if self.settings.environment.lower() in {"prod", "production"} and (
            urlsplit(frontend).scheme != "https"
            or urlsplit(self.settings.auth_public_base_url).scheme != "https"
        ):
            raise AuthProviderError("https_required")
        descriptor = self.host.describe()
        if descriptor is None or (
            descriptor.plugin_id != policy["plugin_id"]
            or descriptor.provider_id != policy["provider_id"]
            or descriptor.provider_namespace != policy["provider_namespace"]
            or descriptor.configuration_generation != policy["config_generation"]
        ):
            raise AuthProviderError("provider_configuration_mismatch")
        self.host.ensure_available()
        return descriptor

    def capabilities(self):
        policy = self.store.get_policy()
        mode = policy["mode"]
        descriptor = self.validate_configuration()
        return {
            "mode": mode,
            "local_login": mode in {"local", "dual", "binding_required"},
            "local_registration": mode in {"local", "dual"},
            "sso_login": mode != "local" and descriptor is not None,
            "binding_allowed": mode in {"dual", "binding_required"},
            "provider_label": descriptor.public_label if descriptor else "统一登录",
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

    def start(self, purpose, browser_proof, *, session_token="", password="", grant_token=""):
        descriptor = self.validate_configuration()
        if descriptor is None:
            raise AuthProviderError("sso_disabled")
        verifier = secrets.token_urlsafe(AUTH_BROWSER_PROOF_BYTES) if descriptor.supports_pkce else ""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() if verifier else None
        state = self.store.begin(
            purpose, browser_proof, ttl_seconds=self.settings.auth_transaction_ttl_seconds,
            session_token=session_token, password=password, pkce_verifier=verifier,
            grant_token=grant_token,
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
        self.validate_configuration()
        return self.store.inspect_completion(
            code, browser_proof, session_seconds=self.settings.auth_sso_session_seconds,
        )

    def confirm(self, pending_id, browser_proof, session_token):
        self.validate_configuration()
        return self.store.confirm(
            pending_id, browser_proof, session_token=session_token,
            session_seconds=self.settings.auth_sso_session_seconds,
        )
